"""Parse supported connection strings without networking or retaining credentials."""

import ipaddress
import re
import secrets
from urllib.parse import parse_qsl, unquote, urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict

from atlas_sql_mcp.config import Connection
from atlas_sql_mcp.domain import SqlError


def _sqlserver_keywords(value: str) -> dict:
    """Parse a bounded ODBC subset; rebuild driver arguments instead of forwarding it."""
    aliases = {
        "server": "host",
        "data source": "host",
        "database": "dbname",
        "initial catalog": "dbname",
        "uid": "user",
        "user id": "user",
        "pwd": "password",
        "password": "password",
        "driver": "driver",
        "encrypt": "encrypt",
        "trustservercertificate": "trust",
        "trust server certificate": "trust",
    }
    raw = {}
    rest = value.strip()
    while rest:
        key, sep, rest = rest.partition("=")
        key = aliases.get(key.strip().lower())
        if not sep or key is None or key in raw:
            raise ValueError
        rest = rest.lstrip()
        if rest.startswith("{"):
            chars = []
            i = 1
            while i < len(rest):
                if rest[i] == "}":
                    if i + 1 < len(rest) and rest[i + 1] == "}":
                        chars.append("}")
                        i += 2
                        continue
                    rest = rest[i + 1 :].lstrip()
                    if rest and not rest.startswith(";"):
                        raise ValueError
                    rest = rest[1:].lstrip() if rest else ""
                    break
                chars.append(rest[i])
                i += 1
            else:
                raise ValueError
            item = "".join(chars)
        else:
            item, _, rest = rest.partition(";")
            item, rest = item.strip(), rest.lstrip()
        raw[key] = item
    if raw.pop("driver", "ODBC Driver 18 for SQL Server").lower() != (
        "odbc driver 18 for sql server"
    ):
        raise ValueError
    if raw.pop("encrypt", "yes").lower() not in {"yes", "true", "mandatory"}:
        raise ValueError
    if raw.pop("trust", "no").lower() not in {"no", "false"}:
        raise ValueError
    server = raw.get("host", "")
    if server.lower().startswith("tcp:"):
        server = server[4:]
    endpoint = re.fullmatch(r"(?:\[([^\]]+)\]|([^,:]+))(?:,([0-9]+))?", server)
    if endpoint is None:
        raise ValueError
    raw["host"] = endpoint[1] or endpoint[2]
    raw["port"] = int(endpoint[3]) if endpoint[3] else 1433
    return raw


def parse_connection(value: str) -> Connection:
    """Parse PostgreSQL conninfo/URIs and the supported SQL Server string formats."""
    try:
        value = value.strip()
        if value.startswith(("postgres://", "postgresql://")) and urlsplit(value).fragment:
            raise ValueError
        if value.startswith(("mssql://", "sqlserver://")):
            uri = urlsplit(value)
            pairs = parse_qsl(uri.query, strict_parsing=True)
            if uri.fragment or len(pairs) != len(dict(pairs)) or set(dict(pairs)) - {"sslmode"}:
                raise ValueError
            raw = {
                "host": uri.hostname,
                "port": 1433 if uri.port is None else uri.port,
                "dbname": unquote(uri.path.removeprefix("/")),
                "user": unquote(uri.username or ""),
                "password": unquote(uri.password or ""),
                **dict(pairs),
            }
            dialect = "sqlserver"
        elif re.match(
            r"(?i)(server|data source|database|initial catalog|uid|user id|pwd|password|driver)\s*=",
            value,
        ):
            raw = _sqlserver_keywords(value)
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
            "Provide a PostgreSQL connection string or SQL Server URI/Server=...;Database=...;UID=...;PWD=... string with host, database, user and password. SQL Server requires ODBC Driver 18, encryption and certificate verification; named instances need an explicit TCP port. Custom options are unsupported.",
            "connectionString",
        ) from None
