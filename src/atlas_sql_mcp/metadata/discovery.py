"""Fixed catalog-only discovery of readable base-table names and columns."""

import time

import psycopg

from atlas_sql_mcp.budget import METADATA_SECONDS, remaining, request_budget
from atlas_sql_mcp.config import Connection
from atlas_sql_mcp.domain import SqlError
from atlas_sql_mcp.metadata.postgres import IDENTITY_SQL, metadata_deadline
from atlas_sql_mcp.metadata.sqlserver import IDENTITY_SQL as TSQL_IDENTITY_SQL
from atlas_sql_mcp.metadata.sqlserver import catalog_deadline, odbc_string

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
ORDER BY n.nspname,c.relname,a.attnum
"""
SQLSERVER_DISCOVERY = """
SELECT s.name,t.name,c.name
FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
JOIN sys.columns c ON c.object_id=t.object_id
WHERE t.is_ms_shipped=0 AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
 AND HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(t.name),
                         'OBJECT','SELECT',c.name,'COLUMN')=1
ORDER BY s.name,t.name,c.column_id
"""


MAX_DISCOVERY_BYTES = 16 * 1024 * 1024


def read_names(cursor, database_major):
    """Bound catalog transfer by bytes, independently of report result cardinality."""
    rows = DiscoveryRows((), database_major)
    size = 0
    fetchmany = getattr(cursor, "fetchmany", None)
    # The fallback supports small injected catalog adapters used by isolated tests.
    batches = iter(lambda: fetchmany(512), []) if fetchmany else (cursor.fetchall(),)
    for batch in batches:
        for row in batch:
            # Allow for tuple/list framing as well as UTF-8 metadata identifier bytes.
            size += 64 + sum(len(value.encode("utf-8")) for value in row)
            if size > MAX_DISCOVERY_BYTES:
                raise SqlError(
                    "METADATA_CAPACITY_EXCEEDED",
                    "Readable schema metadata exceeds the server memory budget; reconnect with a narrower schema scope.",
                )
            rows.append(tuple(row))
    return rows


class DiscoveryRows(list):
    """Readable names plus the database version verified in the same connection."""

    def __init__(self, rows, database_major):
        super().__init__(rows)
        self.database_major = database_major


@request_budget(METADATA_SECONDS)
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
                    # A server-side cursor avoids libpq buffering the whole schema first.
                    with conn.cursor(name="atlas_sql_discovery") as names:
                        names.execute(POSTGRES_DISCOVERY, (list(schemas) or None,) * 2)
                        return read_names(names, version // 10000)
        except psycopg.Error:
            remaining(METADATA_SECONDS)
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
            odbc_string(config), timeout=max(1, int(remaining(METADATA_SECONDS))), autocommit=False
        )
        try:
            conn.timeout = max(1, int(remaining(METADATA_SECONDS)))
            cur = conn.cursor()
            with catalog_deadline(cur, time.monotonic() + remaining(METADATA_SECONDS)):
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
                return read_names(cur, version)
        finally:
            conn.close()
    except pyodbc.Error:
        remaining(METADATA_SECONDS)
        raise SqlError(
            "CONNECTION_UNAVAILABLE",
            "Could not authenticate or read database metadata. Check connection details and TLS.",
            retryable=True,
        ) from None
