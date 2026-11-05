"""Caller-owned, expiring connections. Only fixed catalog SQL reaches the drivers."""

import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from atlas_sql_mcp.config import Connection
from atlas_sql_mcp.connection_strings import parse_connection as parse_connection
from atlas_sql_mcp.domain import Catalog, Column, Principal, Relation, SqlError
from atlas_sql_mcp.metadata.discovery import discover as discover
from atlas_sql_mcp.metadata.postgres import PostgresCatalog
from atlas_sql_mcp.metadata.sqlserver import SqlServerCatalog


@dataclass(frozen=True, repr=False)
class SessionConnection:
    principal: Principal
    config: Connection = field(repr=False)
    expires: float
    storage_bytes: int


MAX_SESSION_METADATA_BYTES = 16 * 1024 * 1024
MAX_RETAINED_METADATA_BYTES = 64 * 1024 * 1024


def grant_storage_bytes(grants):
    """Conservative retained Python-object estimate, including unicode storage."""
    return 512 + sum(
        256
        + 4 * len(key.encode("utf-8"))
        + sum(80 + 4 * len(column.encode("utf-8")) for column in columns)
        for key, columns in grants.items()
    )


class SessionCatalog:
    def __init__(self, configured=None, *, allowed_endpoints=None, clock=time.time):
        self.configured, self.allowed_endpoints, self.clock = configured, allowed_endpoints, clock
        self._sessions = {}
        self._pending = {}
        self._closed = False
        self._lock = threading.RLock()

    def purge_expired(self):
        """Drop idle expired credentials even when clients send no further requests."""
        with self._lock:
            self._purge()

    def close(self):
        """Release retained credentials and reject work after transport shutdown."""
        with self._lock:
            self._closed = True
            self._sessions.clear()

    def _purge(self):
        now = self.clock()
        self._sessions = {k: v for k, v in self._sessions.items() if v.expires > now}

    def _capacity(self, principal):
        if self._closed:
            raise SqlError("CONNECTION_UNAVAILABLE", "The server is shutting down.")
        self._purge()
        if sum(s.storage_bytes for s in self._sessions.values()) >= MAX_RETAINED_METADATA_BYTES:
            raise SqlError(
                "METADATA_CAPACITY_EXCEEDED",
                "The server metadata memory budget is occupied; retry after existing connections expire.",
                retryable=True,
            )
        if (
            len(self._sessions) + sum(self._pending.values()) >= 100
            or sum(s.principal == principal for s in self._sessions.values())
            + self._pending.get(principal, 0)
            >= 10
        ):
            raise SqlError("RATE_LIMITED", "Connection session limit reached.", retryable=True)

    def connect(self, args, principal):
        if self.allowed_endpoints == set():
            raise SqlError(
                "CONNECTION_DISABLED",
                "Connection strings require operator-approved endpoints on this shared server.",
            )
        with self._lock:
            self._capacity(principal)
            self._pending[principal] = self._pending.get(principal, 0) + 1
        try:
            return self._connect(args, principal)
        finally:
            with self._lock:
                self._pending[principal] -= 1
                if not self._pending[principal]:
                    del self._pending[principal]

    def _connect(self, args, principal):
        config = parse_connection(args["connectionString"])
        if (
            self.allowed_endpoints is not None
            and f"{config.host}:{config.port}" not in self.allowed_endpoints
        ):
            raise SqlError(
                "CONNECTION_NOT_AUTHORIZED",
                "This database endpoint is not enabled by the operator.",
            )
        rows = discover(config, args.get("schemas", []))
        schemas = set(args.get("schemas", []))
        grants = {}
        for schema, table, column in rows:
            if schemas and schema not in schemas:
                continue
            if "." in schema or "." in table:
                raise SqlError(
                    "UNSUPPORTED_SQL_FEATURE", "Dots in schema/table identifiers are unsupported."
                )
            grants.setdefault(f"{schema}.{table}", []).append(column)
        if not grants:
            raise SqlError(
                "OBJECT_NOT_AUTHORIZED",
                "No readable base-table columns were found in the requested schemas.",
            )
        storage_bytes = grant_storage_bytes(grants)
        if storage_bytes > MAX_SESSION_METADATA_BYTES:
            raise SqlError(
                "METADATA_CAPACITY_EXCEEDED",
                "Readable schema metadata exceeds the connection memory budget; reconnect with a narrower schema scope.",
            )
        config = replace(config, grants={principal.key: {k: tuple(v) for k, v in grants.items()}})
        expires = self.clock() + 3600
        with self._lock:
            if self._closed:
                raise SqlError("CONNECTION_UNAVAILABLE", "The server is shutting down.")
            self._purge()
            if (
                sum(s.storage_bytes for s in self._sessions.values()) + storage_bytes
                > MAX_RETAINED_METADATA_BYTES
            ):
                raise SqlError(
                    "METADATA_CAPACITY_EXCEEDED",
                    "The server metadata memory budget is occupied; retry after existing connections expire.",
                    retryable=True,
                )
            self._sessions[config.id] = SessionConnection(principal, config, expires, storage_bytes)
        return {
            "status": "connected",
            "connectionId": config.id,
            "dialect": config.dialect,
            "contractVersion": "1.1" if config.dialect == "sqlserver" else "1.0",
            "expiresAt": datetime.fromtimestamp(expires, UTC).isoformat(),
            "relationCount": len(grants),
        }

    def _config(self, principal, connection_id):
        with self._lock:
            if self._closed:
                raise SqlError("CONNECTION_UNAVAILABLE", "The server is shutting down.")
            self._purge()
            session = self._sessions.get(connection_id)
        if session is None:
            if connection_id.startswith("session-"):
                raise SqlError(
                    "CONNECTION_EXPIRED",
                    "This connection expired or the server restarted. Reconnect using your connection string.",
                    "connectionId",
                )
            if self.configured is None:
                raise SqlError(
                    "CONNECTION_NOT_AUTHORIZED",
                    "Connect using your database connection string first.",
                    "connectionId",
                )
            return None
        if session.principal != principal:
            raise SqlError(
                "CONNECTION_NOT_AUTHORIZED", "Connection is unavailable for this identity."
            )
        return session.config

    def load(self, principal, connection_id, relations=None):
        config = self._config(principal, connection_id)
        if config is None:
            if relations is None:
                return self.configured.load(principal, connection_id)
            return self.configured.load(principal, connection_id, relations=relations)
        adapter = SqlServerCatalog if config.dialect == "sqlserver" else PostgresCatalog
        if relations is None:
            return adapter({config.id: config}).load(principal, connection_id)
        return adapter({config.id: config}).load(principal, connection_id, relations=relations)

    def index(self, principal, connection_id):
        """Fresh authorized names only; never use this incomplete catalog for analysis."""
        config = self._config(principal, connection_id)
        if config is None:
            index = getattr(self.configured, "index", self.configured.load)
            return index(principal, connection_id)
        grants = config.authorized(principal)
        rows = discover(config, sorted({key.split(".")[0] for key in grants}))
        current = {}
        for schema, table, column in rows:
            key = f"{schema}.{table}"
            if column in grants.get(key, ()):
                current.setdefault(key, []).append(Column(column, "", True))
        entries = tuple(
            Relation(*key.split("."), tuple(columns)) for key, columns in sorted(current.items())
        )
        major = getattr(rows, "database_major", None)
        if major is None and config.dialect == "sqlserver":
            # Compatibility with injected discovery adapters; real discovery returns the
            # version checked alongside names and privileges.
            major = self.load(principal, connection_id, relations=()).database_major
        return Catalog(
            connection_id,
            major if config.dialect == "sqlserver" else 18,
            entries,
            config.policy_version,
            config.dialect,
        )
