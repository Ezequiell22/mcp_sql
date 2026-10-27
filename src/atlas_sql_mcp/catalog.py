"""Fixed pg_catalog queries only. This adapter has no business-query execution API."""

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg

from .budget import remaining
from .config import Connection, load_connections
from .domain import Catalog, Column, Constraint, Principal, Relation, SqlError

# Names are values, never interpolated SQL. Constraints are disclosed only when all
# participating columns and the referenced relation are authorized.
IDENTITY_SQL = "SELECT current_setting('server_version_num')::integer, rolsuper, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user"
RELATION_SQL = """
SELECT c.oid, c.relkind, pg_catalog.obj_description(c.oid, 'pg_class'),
       pg_catalog.has_table_privilege(c.oid, 'SELECT')
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname=%s AND c.relname=%s AND c.relkind IN ('r','p','v','m')
"""
COLUMN_SQL = """
SELECT a.attname, pg_catalog.format_type(a.atttypid,a.atttypmod), NOT a.attnotnull,
       pg_catalog.col_description(a.attrelid,a.attnum), tn.nspname,
       pg_catalog.has_column_privilege(a.attrelid,a.attnum,'SELECT')
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace
WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum
"""
CONSTRAINT_SQL = """
SELECT co.conname, co.contype,
 ARRAY(SELECT a.attname FROM unnest(co.conkey) WITH ORDINALITY k(num,ord)
       JOIN pg_catalog.pg_attribute a ON a.attrelid=co.conrelid AND a.attnum=k.num ORDER BY k.ord),
 rn.nspname, rc.relname,
 ARRAY(SELECT a.attname FROM unnest(co.confkey) WITH ORDINALITY k(num,ord)
       JOIN pg_catalog.pg_attribute a ON a.attrelid=co.confrelid AND a.attnum=k.num ORDER BY k.ord),
 co.convalidated, co.conenforced
FROM pg_catalog.pg_constraint co
LEFT JOIN pg_catalog.pg_class rc ON rc.oid=co.confrelid
LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid=rc.relnamespace
WHERE co.conrelid=%s AND co.contype IN ('p','u','f') ORDER BY co.conname,co.oid
"""
DESCENDANTS_SQL = """
WITH RECURSIVE children(oid) AS (
 SELECT inhrelid FROM pg_catalog.pg_inherits WHERE inhparent=%s
 UNION SELECT i.inhrelid FROM pg_catalog.pg_inherits i JOIN children c ON i.inhparent=c.oid
)
SELECT n.nspname || '.' || c.relname FROM children d
JOIN pg_catalog.pg_class c ON c.oid=d.oid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace ORDER BY 1
"""


class PostgresCatalog:
    def __init__(self, connections: dict[str, Connection] | Path):
        self.connections = connections

    def load(self, principal: Principal, connection_id: str) -> Catalog:
        try:
            connections = (
                load_connections(self.connections)
                if isinstance(self.connections, Path)
                else self.connections
            )
        except (ValueError, OSError):
            raise SqlError(
                "CONNECTION_POLICY_UNAVAILABLE", "Current connection policy could not be loaded."
            ) from None
        config = connections.get(connection_id)
        if not config:
            raise SqlError(
                "CONNECTION_NOT_AUTHORIZED", "Connection is unavailable for this identity."
            )
        grants = config.authorized(principal)
        if config.dialect != "postgresql":
            raise SqlError("UNSUPPORTED_DIALECT", "PostgreSQL adapter received another dialect.")
        try:
            kwargs = config.connect_kwargs()
            # libpq enforces a minimum of two seconds for connect_timeout.
            connection_budget = remaining(3)
            if connection_budget < 2:
                raise SqlError(
                    "VALIDATION_TIMEOUT",
                    "Insufficient budget for a new catalog connection.",
                    retryable=True,
                )
            kwargs["connect_timeout"] = int(connection_budget)
            with psycopg.connect(**kwargs) as conn:
                conn.read_only = True
                conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                with metadata_deadline(conn), conn.cursor() as cur:
                    cur.execute(IDENTITY_SQL)
                    version, superuser, bypass = cur.fetchone()
                    if superuser or bypass:
                        raise SqlError(
                            "CONNECTION_POLICY_VIOLATION",
                            "Catalog role must not be superuser or bypass RLS.",
                        )
                    if version // 10000 != 18:
                        raise SqlError(
                            "UNSUPPORTED_DATABASE_VERSION",
                            "This analyzer is tested for PostgreSQL 18 only.",
                        )
                    relations = []
                    started = time.monotonic()
                    for key, allowed_columns in sorted(grants.items()):
                        schema, name = key.split(".")
                        if time.monotonic() - started > 3:
                            raise SqlError(
                                "VALIDATION_TIMEOUT",
                                "Catalog reading exceeded its total time budget.",
                                retryable=True,
                            )
                        cur.execute(RELATION_SQL, (schema, name))
                        row = cur.fetchone()
                        if not row:
                            raise SqlError(
                                "CONTEXT_STALE", "An authorized relation is no longer available."
                            )
                        oid, kind, comment, _ = row
                        if kind not in {"r", "p"}:
                            raise SqlError(
                                "UNSUPPORTED_SQL_FEATURE",
                                "Views require dependency attestations; configure base tables for this version.",
                            )
                        cur.execute(COLUMN_SQL, (oid,))
                        columns = tuple(
                            Column(n, t, nullable, (desc or "")[:4096], ts)
                            for n, t, nullable, desc, ts, granted in cur.fetchall()
                            if n in allowed_columns and granted
                        )
                        if set(allowed_columns) != {c.name for c in columns}:
                            raise SqlError(
                                "OBJECT_NOT_AUTHORIZED",
                                "An authorized column is unavailable or its database grant changed.",
                            )
                        cur.execute(CONSTRAINT_SQL, (oid,))
                        constraints = []
                        for (
                            cn,
                            ck,
                            cols,
                            target_schema,
                            target,
                            target_cols,
                            valid,
                            enforced,
                        ) in cur.fetchall():
                            if not set(cols) <= set(allowed_columns):
                                continue
                            if ck == "f" and not set(target_cols) <= set(
                                grants.get(f"{target_schema}.{target}", ())
                            ):
                                continue
                            constraints.append(
                                Constraint(
                                    cn,
                                    ck,
                                    tuple(cols),
                                    target_schema or "",
                                    target or "",
                                    tuple(target_cols),
                                    valid,
                                    enforced,
                                )
                            )
                        cur.execute(DESCENDANTS_SQL, (oid,))
                        descendants = tuple(r[0] for r in cur.fetchall())
                        # Never disclose names outside the configured policy.
                        if any(d not in grants for d in descendants):
                            descendants = ("<unauthorized-descendants>",)
                        relations.append(
                            Relation(
                                schema,
                                name,
                                columns,
                                tuple(constraints),
                                (comment or "")[:4096],
                                "table",
                                descendants,
                            )
                        )
                    return Catalog(connection_id, 18, tuple(relations), config.policy_version)
        except SqlError:
            raise
        except psycopg.Error:
            raise SqlError(
                "CONNECTION_UNAVAILABLE", "Authorized metadata could not be read.", retryable=True
            ) from None


@contextmanager
def metadata_deadline(connection):
    """Cancel a long metadata operation as a whole, not just each statement."""
    expired = threading.Event()

    def cancel():
        expired.set()
        try:
            connection.cancel_safe(timeout=1)
        except psycopg.Error:
            pass

    timer = threading.Timer(remaining(3), cancel)
    timer.daemon = True
    timer.start()
    try:
        yield
        if expired.is_set():
            raise SqlError(
                "VALIDATION_TIMEOUT",
                "Catalog reading exceeded its total time budget.",
                retryable=True,
            )
    finally:
        timer.cancel()
