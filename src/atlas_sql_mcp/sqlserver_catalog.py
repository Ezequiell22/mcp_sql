"""SQL Server catalog-only adapter. No caller-provided SQL reaches this driver."""

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .budget import remaining
from .catalog import PostgresCatalog
from .config import Connection, load_connections
from .domain import Catalog, Column, Constraint, Principal, Relation, SqlError

IDENTITY_SQL = """
SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int),
       IS_SRVROLEMEMBER('sysadmin'), IS_MEMBER('db_owner'),
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CONTROL')
"""
RELATION_SQL = """
SELECT t.object_id, t.temporal_type, t.is_filetable, t.ledger_type,
       CASE WHEN e.object_id IS NULL THEN 0 ELSE 1 END
FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
LEFT JOIN sys.external_tables e ON e.object_id=t.object_id
WHERE s.name=? AND t.name=? AND t.is_ms_shipped=0
"""
COLUMN_SQL = """
SELECT c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable,
       c.is_computed, c.is_hidden, c.encryption_type, ty.is_user_defined,
       HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(t.name),
                         'OBJECT', 'SELECT', c.name, 'COLUMN'),
       CASE WHEN m.is_masked=1 THEN 1 ELSE 0 END
FROM sys.columns c JOIN sys.tables t ON t.object_id=c.object_id
JOIN sys.schemas s ON s.schema_id=t.schema_id
JOIN sys.types ty ON ty.user_type_id=c.user_type_id
LEFT JOIN sys.masked_columns m ON m.object_id=c.object_id AND m.column_id=c.column_id
WHERE c.object_id=? ORDER BY c.column_id
"""
KEY_SQL = """
SELECT i.name, CASE WHEN i.is_primary_key=1 THEN 'p' ELSE 'u' END, c.name
FROM sys.indexes i JOIN sys.index_columns k
 ON k.object_id=i.object_id AND k.index_id=i.index_id
JOIN sys.columns c ON c.object_id=k.object_id AND c.column_id=k.column_id
WHERE i.object_id=? AND i.is_unique=1 AND i.is_disabled=0
 AND i.is_hypothetical=0 AND i.has_filter=0 AND k.key_ordinal>0
ORDER BY i.index_id, k.key_ordinal
"""
FK_SQL = """
SELECT f.name, c.name, s.name, t.name, rc.name, f.is_not_trusted, f.is_disabled
FROM sys.foreign_keys f JOIN sys.foreign_key_columns k ON k.constraint_object_id=f.object_id
JOIN sys.columns c ON c.object_id=k.parent_object_id AND c.column_id=k.parent_column_id
JOIN sys.tables t ON t.object_id=k.referenced_object_id
JOIN sys.schemas s ON s.schema_id=t.schema_id
JOIN sys.columns rc ON rc.object_id=k.referenced_object_id AND rc.column_id=k.referenced_column_id
WHERE f.parent_object_id=? ORDER BY f.object_id, k.constraint_column_id
"""


def odbc_string(config: Connection) -> str:
    """Brace every value, including credentials, to prevent ODBC option injection."""
    password = os.environ.get(config.password_env)
    if not password:
        raise SqlError("CONNECTION_UNAVAILABLE", "Configured credential is unavailable.")

    def quoted(value):
        return "{" + str(value).replace("}", "}}") + "}"

    values = {
        "DRIVER": "ODBC Driver 18 for SQL Server",
        "SERVER": f"tcp:{config.host},{config.port}",
        "DATABASE": config.dbname,
        "UID": config.user,
        "PWD": password,
        "Encrypt": "yes",
        "TrustServerCertificate": "no",
        "ApplicationIntent": "ReadOnly",
        "APP": "atlas_sql_mcp_catalog",
    }
    return ";".join(f"{k}={quoted(v)}" for k, v in values.items())


def column_type(name, length, precision, scale):
    if name in {"decimal", "numeric"}:
        return f"decimal({precision},{scale})"
    if name in {"varchar", "nvarchar", "char", "nchar"}:
        size = length // 2 if name.startswith("n") and length != -1 else length
        return f"{name}({'max' if size == -1 else size})"
    if name in {"datetime2", "datetimeoffset"}:
        return f"{name}({scale})"
    return name


@contextmanager
def catalog_deadline(cursor, deadline):
    expired = threading.Event()

    def cancel():
        expired.set()
        try:
            cursor.cancel()
        except Exception:
            pass  # Never include a driver error or connection string in diagnostics.

    def check():
        if expired.is_set() or time.monotonic() >= deadline:
            raise SqlError("VALIDATION_TIMEOUT", "Catalog deadline exceeded.", retryable=True)

    timer = threading.Timer(max(0, deadline - time.monotonic()), cancel)
    timer.daemon = True
    timer.start()
    try:
        check()
        yield check
        check()
    finally:
        timer.cancel()


class SqlServerCatalog:
    def __init__(self, connections: dict[str, Connection] | Path):
        self.connections = connections

    def load(self, principal: Principal, connection_id: str) -> Catalog:
        try:
            configs = (
                load_connections(self.connections)
                if isinstance(self.connections, Path)
                else self.connections
            )
        except (ValueError, OSError):
            raise SqlError(
                "CONNECTION_POLICY_UNAVAILABLE", "Connection policy could not be loaded."
            ) from None
        config = configs.get(connection_id)
        if not config:
            raise SqlError(
                "CONNECTION_NOT_AUTHORIZED", "Connection is unavailable for this identity."
            )
        grants = config.authorized(principal)
        if config.dialect != "sqlserver":
            raise SqlError("UNSUPPORTED_DIALECT", "SQL Server adapter received another dialect.")
        try:
            import pyodbc
        except ImportError:
            raise SqlError(
                "DRIVER_UNAVAILABLE", "Install the sqlserver extra and Microsoft ODBC Driver 18."
            ) from None
        conn = None
        deadline = time.monotonic() + remaining(3)
        try:
            conn = pyodbc.connect(
                odbc_string(config), timeout=max(1, int(remaining(3))), autocommit=False
            )
            conn.timeout = max(1, int(remaining(3)))
            cur = conn.cursor()
            with catalog_deadline(cur, deadline) as check_deadline:

                def read(statement, parameters=()):
                    check_deadline()
                    cur.execute(statement, parameters)
                    check_deadline()

                read(IDENTITY_SQL)
                version, admin, owner, control = cur.fetchone()
                if any(v != 0 for v in (admin, owner, control)):
                    raise SqlError(
                        "CONNECTION_POLICY_VIOLATION", "Catalog role must be a restricted reader."
                    )
                if version not in {16, 17}:
                    raise SqlError(
                        "UNSUPPORTED_DATABASE_VERSION",
                        "SQL Server metadata adapter targets major versions 16 and 17.",
                    )
                relations = []
                for key, allowed in sorted(grants.items()):
                    check_deadline()
                    schema, name = key.split(".")
                    read(RELATION_SQL, (schema, name))
                    row = cur.fetchone()
                    if not row:
                        raise SqlError(
                            "OBJECT_NOT_AUTHORIZED", "An authorized base table is unavailable."
                        )
                    oid, *special = row
                    if any(special):
                        raise SqlError(
                            "UNSUPPORTED_SQL_FEATURE",
                            "Temporal, ledger, external and file tables are unsupported.",
                        )
                    read(COLUMN_SQL, (oid,))
                    columns = []
                    for (
                        n,
                        ty,
                        size,
                        precision,
                        scale,
                        nullable,
                        computed,
                        hidden,
                        encrypted,
                        custom,
                        permitted,
                        masked,
                    ) in cur.fetchall():
                        if n not in allowed:
                            continue
                        if permitted != 1:
                            raise SqlError(
                                "OBJECT_NOT_AUTHORIZED", "A configured column is unavailable."
                            )
                        if any((computed, hidden, encrypted, custom, masked)):
                            raise SqlError(
                                "UNSUPPORTED_SQL_FEATURE",
                                "Computed, hidden, encrypted, masked and custom-type columns are unsupported.",
                            )
                        columns.append(
                            Column(
                                n,
                                column_type(ty, size, precision, scale),
                                bool(nullable),
                                type_schema="sys",
                            )
                        )
                    if set(allowed) != {c.name for c in columns}:
                        raise SqlError(
                            "OBJECT_NOT_AUTHORIZED", "A configured column is unavailable."
                        )
                    constraints = []
                    read(KEY_SQL, (oid,))
                    groups = {}
                    for cn, kind, col in cur.fetchall():
                        groups.setdefault((cn, kind), []).append(col)
                    for (cn, kind), cols in groups.items():
                        if set(cols) <= set(allowed):
                            constraints.append(Constraint(cn, kind, tuple(cols)))
                    read(FK_SQL, (oid,))
                    groups = {}
                    for (
                        cn,
                        col,
                        target_schema,
                        target,
                        target_col,
                        untrusted,
                        disabled,
                    ) in cur.fetchall():
                        group = groups.setdefault(
                            (cn, target_schema, target, untrusted, disabled), ([], [])
                        )
                        group[0].append(col)
                        group[1].append(target_col)
                    for (cn, ts, target, untrusted, disabled), (cols, targets) in groups.items():
                        if set(cols) <= set(allowed) and set(targets) <= set(
                            grants.get(f"{ts}.{target}", ())
                        ):
                            constraints.append(
                                Constraint(
                                    cn,
                                    "f",
                                    tuple(cols),
                                    ts,
                                    target,
                                    tuple(targets),
                                    not untrusted,
                                    not disabled,
                                )
                            )
                    relations.append(Relation(schema, name, tuple(columns), tuple(constraints)))
                return Catalog(
                    connection_id, 0, tuple(relations), config.policy_version, "sqlserver", version
                )
        except SqlError:
            raise
        except pyodbc.Error:
            raise SqlError(
                "CONNECTION_UNAVAILABLE", "Authorized metadata could not be read.", retryable=True
            ) from None
        finally:
            if conn is not None:
                for cleanup in (conn.rollback, conn.close):
                    try:
                        cleanup()
                    except pyodbc.Error:
                        pass  # Never replace a sanitized result with a cleanup diagnostic.


class DialectCatalog:
    """Reload policy on each call; choose an adapter from operator configuration."""

    def __init__(self, connections: Path):
        self.connections = connections

    def load(self, principal: Principal, connection_id: str) -> Catalog:
        try:
            configs = load_connections(self.connections)
        except (ValueError, OSError):
            raise SqlError(
                "CONNECTION_POLICY_UNAVAILABLE", "Connection policy could not be loaded."
            ) from None
        config = configs.get(connection_id)
        if not config:
            raise SqlError(
                "CONNECTION_NOT_AUTHORIZED", "Connection is unavailable for this identity."
            )
        adapter = SqlServerCatalog if config.dialect == "sqlserver" else PostgresCatalog
        return adapter(configs).load(principal, connection_id)
