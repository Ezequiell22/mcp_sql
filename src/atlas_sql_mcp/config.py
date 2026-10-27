"""Operator configuration and private in-memory connection credentials."""

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .domain import Principal, SqlError, digest


@dataclass(frozen=True)
class Connection:
    id: str
    host: str
    port: int
    dbname: str
    user: str
    password_env: str
    sslmode: str
    grants: dict[str, dict[str, tuple[str, ...]]]
    dialect: str = "postgresql"
    password: str | None = field(default=None, repr=False, compare=False)

    @property
    def policy_version(self) -> str:
        return digest(
            {
                "dialect": self.dialect,
                "id": self.id,
                "host": self.host,
                "port": self.port,
                "database": self.dbname,
                "user": self.user,
                "sslmode": self.sslmode,
                "grants": self.grants,
            }
        )

    def authorized(self, principal: Principal) -> dict[str, tuple[str, ...]]:
        grants = self.grants.get(principal.key)
        if not grants:
            raise SqlError(
                "CONNECTION_NOT_AUTHORIZED", "Connection is unavailable for this identity."
            )
        return grants

    def connect_kwargs(self) -> dict:
        password = (
            self.password
            if self.password is not None
            else (os.environ.get(self.password_env) if self.password_env else None)
        )
        if self.password_env and not password:
            raise SqlError(
                "CONNECTION_UNAVAILABLE", "Configured credential is unavailable.", retryable=True
            )
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": password,
            "sslmode": self.sslmode,
            "connect_timeout": 3,
            "application_name": "atlas_sql_mcp_catalog",
            "options": "-c default_transaction_read_only=on -c statement_timeout=3000 -c search_path=pg_catalog",
        }


def load_connections(path: Path) -> dict[str, Connection]:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Connection configuration exceeds 2 MiB.")
    data = json.loads(path.read_text())
    if set(data) != {"connections"} or not isinstance(data["connections"], list):
        raise ValueError("Configuration must contain a connections array only.")
    result = {}
    required = {"id", "host", "port", "dbname", "user", "passwordEnv", "sslmode", "grants"}
    for raw in data["connections"]:
        if set(raw) not in (required, required | {"dialect"}):
            raise ValueError("Connection configuration has missing or unknown fields.")
        if any(
            not isinstance(raw[k], str) or not raw[k] or len(raw[k]) > 256
            for k in ("id", "host", "dbname", "user")
        ):
            raise ValueError("Connection identifiers must be bounded nonempty strings.")
        if not isinstance(raw["passwordEnv"], str) or (
            raw["passwordEnv"] and not raw["passwordEnv"].isidentifier()
        ):
            raise ValueError(
                "passwordEnv must name a credential variable, or be empty for libpq passfile authentication."
            )
        if type(raw["port"]) is not int or not 1 <= raw["port"] <= 65535:
            raise ValueError("Invalid database port.")
        try:
            loopback = ipaddress.ip_address(raw["host"]).is_loopback
        except ValueError:
            loopback = False
        dialect = raw.get("dialect", "postgresql")
        if dialect not in {"postgresql", "sqlserver"}:
            raise ValueError("Unsupported database dialect.")
        if dialect == "sqlserver" and (raw["sslmode"] != "verify-full" or not raw["passwordEnv"]):
            raise ValueError("SQL Server requires verified TLS and a passwordEnv reference.")
        if raw["sslmode"] != "verify-full" and not (
            loopback and raw["sslmode"] in {"disable", "require"}
        ):
            raise ValueError("Non-loopback PostgreSQL connections require sslmode=verify-full.")
        grants = raw["grants"]
        if not isinstance(grants, dict) or not grants:
            raise ValueError("Explicit per-principal relation/column grants are required.")
        frozen_grants = {}
        for identity, relations in grants.items():
            if (
                not isinstance(identity, str)
                or "#" not in identity
                or not isinstance(relations, dict)
                or not relations
                or len(relations) > 1000
            ):
                raise ValueError("Grant identity must be issuer#subject with explicit relations.")
            normalized = {}
            for relation, columns in relations.items():
                if (
                    not isinstance(relation, str)
                    or relation.count(".") != 1
                    or not all(relation.split("."))
                ):
                    raise ValueError(
                        "Use explicit schema.relation grant keys; dots in names are unsupported."
                    )
                if (
                    not isinstance(columns, list)
                    or not columns
                    or len(columns) > 10000
                    or any(not isinstance(c, str) or not c or c == "*" for c in columns)
                ):
                    raise ValueError(
                        "Explicit column names are required; wildcard grants are forbidden."
                    )
                normalized[relation] = tuple(sorted(set(columns)))
            frozen_grants[identity] = normalized
        connection = Connection(
            raw["id"],
            raw["host"],
            raw["port"],
            raw["dbname"],
            raw["user"],
            raw["passwordEnv"],
            raw["sslmode"],
            frozen_grants,
            dialect,
        )
        if connection.id in result:
            raise ValueError("Duplicate connection identifier.")
        result[connection.id] = connection
    if not result or len(result) > 100:
        raise ValueError("Configure between 1 and 100 connections.")
    return result


def validate_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Shared authentication endpoints require HTTPS without credentials, query, or fragment."
        )
    return value.rstrip("/")
