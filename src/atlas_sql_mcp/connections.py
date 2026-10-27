"""Caller-owned, expiring connections. Only fixed catalog SQL reaches the drivers."""

import ipaddress
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from urllib.parse import parse_qsl, unquote, urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict

from .budget import remaining
from .catalog import IDENTITY_SQL, PostgresCatalog, metadata_deadline
from .config import Connection
from .domain import Principal, SqlError
from .sqlserver_catalog import (
    IDENTITY_SQL as TSQL_IDENTITY_SQL,
)
from .sqlserver_catalog import (
    SqlServerCatalog,
    catalog_deadline,
    odbc_string,
)

# No business rows, custom SQL, views, system schemas, or caller-selected SQL options.
POSTGRES_DISCOVERY = """
SELECT n.nspname, c.relname, a.attname
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
WHERE c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped
 AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
 AND (%s::text[] IS NULL OR n.nspname=ANY(%s::text[]))
 AND pg_catalog.has_schema_privilege(n.oid,'USAGE')
 AND pg_catalog.has_column_privilege(c.oid,a.attnum,'SELECT')
ORDER BY n.nspname,c.relname,a.attnum LIMIT 10001
"""
SQLSERVER_DISCOVERY = """
SELECT TOP(10001) s.name,t.name,c.name
FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
JOIN sys.columns c ON c.object_id=t.object_id
WHERE t.is_ms_shipped=0 AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
 AND HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(t.name),
                         'OBJECT','SELECT',c.name,'COLUMN')=1
ORDER BY s.name,t.name,c.column_id
"""


def parse_connection(value: str) -> Connection:
    """Use libpq's parser; SQL Server supports mssql/sqlserver URI credentials."""
    try:
        if value.startswith(("postgres://", "postgresql://")) and urlsplit(value).fragment:
            raise ValueError
        if value.startswith(("mssql://", "sqlserver://")):
            uri = urlsplit(value)
            pairs = parse_qsl(uri.query, strict_parsing=True)
            if uri.fragment or len(pairs) != len(dict(pairs)):
                raise ValueError
            raw = {
                "host": uri.hostname,
                "port": uri.port or 1433,
                "dbname": unquote(uri.path.removeprefix("/")),
                "user": unquote(uri.username or ""),
                "password": unquote(uri.password or ""),
                **dict(pairs),
            }
            dialect = "sqlserver"
        else:
            raw = conninfo_to_dict(value)
            dialect = "postgresql"
        if set(raw) - {"host", "port", "dbname", "user", "password", "sslmode"}:
            raise ValueError
        if any(
            not isinstance(raw.get(k), str) or not raw[k] or "\x00" in raw[k]
            for k in ("host", "dbname", "user", "password")
        ):
            raise ValueError
        host = raw["host"]
        if any(x in host for x in (",", "/", "\\", "=", ";")) or any(x.isspace() for x in host):
            raise ValueError
        if any(len(raw[k]) > 256 for k in ("host", "dbname", "user")):
            raise ValueError
        if "://" in raw["dbname"] or "=" in raw["dbname"]:
            raise ValueError
        port = int(raw.get("port", 5432))
        if not 1 <= port <= 65535:
            raise ValueError
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        sslmode = raw.get(
            "sslmode", "disable" if loopback and dialect == "postgresql" else "verify-full"
        )
        if sslmode != "verify-full" and not (
            loopback and dialect == "postgresql" and sslmode in {"disable", "require"}
        ):
            raise ValueError
        return Connection(
            "session-" + secrets.token_urlsafe(24),
            host,
            port,
            raw["dbname"],
            raw["user"],
            "",
            sslmode,
            {},
            dialect,
            raw["password"],
        )
    except (ValueError, TypeError, psycopg.Error):
        raise SqlError(
            "INVALID_CONNECTION_STRING",
            "Provide a PostgreSQL URI/conninfo or SQL Server URI with host, database, user and password. Remote connections require verified TLS; custom driver options are unsupported.",
            "connectionString",
        ) from None


def discover(config: Connection, schemas=()) -> list:
    if config.dialect == "postgresql":
        try:
            with psycopg.connect(**config.connect_kwargs()) as conn:
                conn.read_only = True
                conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                with metadata_deadline(conn), conn.cursor() as cur:
                    cur.execute(IDENTITY_SQL)
                    version, admin, bypass = cur.fetchone()
                    if admin or bypass:
                        raise SqlError(
                            "CONNECTION_POLICY_VIOLATION", "Use a restricted database reader."
                        )
                    if version // 10000 != 18:
                        raise SqlError("UNSUPPORTED_DATABASE_VERSION", "PostgreSQL 18 is required.")
                    cur.execute(POSTGRES_DISCOVERY, (list(schemas) or None,) * 2)
                    return cur.fetchall()
        except psycopg.Error:
            raise SqlError(
                "CONNECTION_UNAVAILABLE",
                "Could not authenticate or read database metadata. Check the host, port, database, credentials and TLS.",
                retryable=True,
            ) from None
    try:
        import pyodbc
    except ImportError:
        raise SqlError(
            "DRIVER_UNAVAILABLE", "Install the SQL Server extra and ODBC Driver 18."
        ) from None
    try:
        conn = pyodbc.connect(
            odbc_string(config), timeout=max(1, int(remaining(3))), autocommit=False
        )
        try:
            conn.timeout = max(1, int(remaining(3)))
            cur = conn.cursor()
            with catalog_deadline(cur, time.monotonic() + remaining(3)):
                cur.execute(TSQL_IDENTITY_SQL)
                version, admin, owner, control = cur.fetchone()
                if any(v != 0 for v in (admin, owner, control)):
                    raise SqlError(
                        "CONNECTION_POLICY_VIOLATION", "Use a restricted database reader."
                    )
                if version not in {16, 17}:
                    raise SqlError("UNSUPPORTED_DATABASE_VERSION", "SQL Server 16/17 is required.")
                statement = SQLSERVER_DISCOVERY
                if schemas:
                    statement = statement.replace(
                        "ORDER BY",
                        "AND s.name IN (" + ",".join("?" for _ in schemas) + ") ORDER BY",
                    )
                cur.execute(statement, tuple(schemas))
                return cur.fetchall()
        finally:
            conn.close()
    except pyodbc.Error:
        raise SqlError(
            "CONNECTION_UNAVAILABLE",
            "Could not authenticate or read database metadata. Check connection details and TLS.",
            retryable=True,
        ) from None


@dataclass(frozen=True, repr=False)
class SessionConnection:
    principal: Principal
    config: Connection = field(repr=False)
    expires: float


class SessionCatalog:
    def __init__(self, configured, *, allowed_endpoints=None, clock=time.time):
        self.configured, self.allowed_endpoints, self.clock = configured, allowed_endpoints, clock
        self._sessions = {}
        self._lock = threading.RLock()

    def _purge(self):
        now = self.clock()
        self._sessions = {k: v for k, v in self._sessions.items() if v.expires > now}

    def _capacity(self, principal):
        self._purge()
        if (
            len(self._sessions) >= 100
            or sum(s.principal == principal for s in self._sessions.values()) >= 10
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
        if len(rows) > 10000:
            raise SqlError("INPUT_TOO_LARGE", "Database exceeds the discovery column budget.")
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
        if len(grants) > 1000:
            raise SqlError("INPUT_TOO_LARGE", "Database exceeds the discovery table budget.")
        config = replace(config, grants={principal.key: {k: tuple(v) for k, v in grants.items()}})
        adapter = SqlServerCatalog if config.dialect == "sqlserver" else PostgresCatalog
        adapter({config.id: config}).load(principal, config.id)
        expires = self.clock() + 3600
        with self._lock:
            self._capacity(principal)
            self._sessions[config.id] = SessionConnection(principal, config, expires)
        return {
            "status": "connected",
            "connectionId": config.id,
            "dialect": config.dialect,
            "contractVersion": "1.1" if config.dialect == "sqlserver" else "1.0",
            "expiresAt": datetime.fromtimestamp(expires, UTC).isoformat(),
            "relationCount": len(grants),
        }

    def load(self, principal, connection_id):
        with self._lock:
            self._purge()
            session = self._sessions.get(connection_id)
        if session is None:
            return self.configured.load(principal, connection_id)
        if session.principal != principal:
            raise SqlError(
                "CONNECTION_NOT_AUTHORIZED", "Connection is unavailable for this identity."
            )
        config = session.config
        adapter = SqlServerCatalog if config.dialect == "sqlserver" else PostgresCatalog
        return adapter({config.id: config}).load(principal, connection_id)
