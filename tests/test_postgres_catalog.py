"""Scope metadata detail reads without touching unrelated database objects."""

from contextlib import nullcontext

import pytest

from atlas_sql_mcp.config import Connection
from atlas_sql_mcp.domain import Principal, SqlError
from atlas_sql_mcp.metadata import postgres


def test_scoped_postgres_metadata_reads_only_requested_relation(monkeypatch):
    calls = []
    rows = {
        postgres.IDENTITY_SQL: [(180000, False, False)],
        postgres.RELATION_SQL: [(42, "r", "", True)],
        postgres.COLUMN_SQL: [("id", "integer", False, "", "pg_catalog", True)],
        postgres.CONSTRAINT_SQL: [
            ("outside_fk", "f", ["id"], "private", "hidden", ["id"], True, True),
        ],
        postgres.DESCENDANTS_SQL: [],
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, parameters=()):
            calls.append((statement, parameters))
            self.current = rows[statement]

        def fetchone(self):
            return self.current[0]

        def fetchall(self):
            return self.current

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(postgres.psycopg, "connect", lambda **kwargs: Database())
    monkeypatch.setattr(postgres, "metadata_deadline", lambda connection: nullcontext())
    config = Connection(
        "sales",
        "localhost",
        5432,
        "sales",
        "reader",
        "",
        "disable",
        {"local#analyst": {"public.orders": ("id",), "private.hidden": ("id",)}},
    )
    adapter = postgres.PostgresCatalog({"sales": config})
    result = adapter.load(Principal("local", "analyst"), "sales", relations=("public.orders",))
    assert [relation.key for relation in result.relations] == ["public.orders"]
    assert "outside_fk" not in repr(result)
    assert [parameters for query, parameters in calls if query == postgres.RELATION_SQL] == [
        ("public", "orders")
    ]
    calls.clear()
    with pytest.raises(SqlError) as error:
        adapter.load(Principal("local", "analyst"), "sales", relations=("private.unknown",))
    assert error.value.code == "OBJECT_NOT_AUTHORIZED"
    assert not calls
