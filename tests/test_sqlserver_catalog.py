"""Exercise fixed catalog statements, authorization and driver error redaction."""

import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from atlas_sql_mcp.config import Connection, load_connections
from atlas_sql_mcp.domain import Principal, SqlError
from atlas_sql_mcp.sqlserver_catalog import (
    COLUMN_SQL,
    FK_SQL,
    IDENTITY_SQL,
    KEY_SQL,
    RELATION_SQL,
    DialectCatalog,
    SqlServerCatalog,
    column_type,
    odbc_string,
)


@pytest.fixture
def config():
    return Connection(
        "sales",
        "db.example.test",
        1433,
        "sales",
        "reader",
        "ATLAS_TEST_SECRET",
        "verify-full",
        {"local#analyst": {"dbo.orders": ("id", "amount")}},
        "sqlserver",
    )


class DriverError(Exception):
    pass


class Cursor:
    def __init__(self):
        self.calls = []
        self.rows = {
            IDENTITY_SQL: [(16, 0, 0, 0)],
            RELATION_SQL: [(42, 0, 0, 0, 0)],
            COLUMN_SQL: [
                ("id", "int", 4, 10, 0, False, False, False, None, False, 1, 0),
                ("amount", "decimal", 9, 20, 2, False, False, False, None, False, 1, 0),
            ],
            KEY_SQL: [("pk", "p", "id")],
            FK_SQL: [("secret_fk", "id", "private", "hidden", "id", False, False)],
        }

    def execute(self, statement, parameters=()):
        assert statement in self.rows  # No generated SQL or helper statement can reach the driver.
        self.calls.append((statement, parameters))
        self.current = self.rows[statement]

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return self.current

    def cancel(self):
        pass


@pytest.fixture
def driver(monkeypatch):
    cursor = Cursor()
    conn = SimpleNamespace(cursor=lambda: cursor, rollback=lambda: None, close=lambda: None)
    connections = []

    def connect(value, **kwargs):
        connections.append((value, kwargs))
        return conn

    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(connect=connect, Error=DriverError))
    monkeypatch.setenv("ATLAS_TEST_SECRET", "synthetic-only;PWD=evil}value")
    return cursor, connections


def test_fixed_catalog_and_hidden_relationships(config, driver):
    cursor, connects = driver
    c = SqlServerCatalog({"sales": config}).load(Principal("local", "analyst"), "sales")
    assert c.dialect == "sqlserver" and c.database_major == 16
    relation = c.relations[0]
    assert [col.postgres_type for col in relation.columns] == ["int", "decimal(20,2)"]
    assert [key.name for key in relation.constraints] == ["pk"]
    assert "hidden" not in repr(c)
    assert len(cursor.calls) == 5
    assert "Encrypt={yes}" in connects[0][0]
    assert "TrustServerCertificate={no}" in connects[0][0]
    assert "PWD={synthetic-only;PWD=evil}}value}" in connects[0][0]


@pytest.mark.parametrize(
    "identity", [(16, 1, 0, 0), (16, 0, 1, 0), (16, 0, 0, 1), (16, None, 0, 0), (15, 0, 0, 0)]
)
def test_elevated_unknown_and_unsupported_identity_fail(config, driver, identity):
    driver[0].rows[IDENTITY_SQL] = [identity]
    with pytest.raises(SqlError):
        SqlServerCatalog({"sales": config}).load(Principal("local", "analyst"), "sales")
    assert len(driver[0].calls) == 1


@pytest.mark.parametrize("index,value", [(6, True), (7, True), (8, 1), (9, True), (10, 0), (11, 1)])
def test_unsafe_or_revoked_columns_fail(config, driver, index, value):
    row = list(driver[0].rows[COLUMN_SQL][0])
    row[index] = value
    driver[0].rows[COLUMN_SQL][0] = tuple(row)
    with pytest.raises(SqlError):
        SqlServerCatalog({"sales": config}).load(Principal("local", "analyst"), "sales")


def test_unknown_identity_does_not_connect(config, driver):
    with pytest.raises(SqlError):
        SqlServerCatalog({"sales": config}).load(Principal("local", "other"), "sales")
    assert not driver[1]


def test_redacts_driver_diagnostics(config, driver, monkeypatch):
    def broken(*args, **kwargs):
        raise DriverError("password=NEVER_RETURN secret database path")

    monkeypatch.setattr(sys.modules["pyodbc"], "connect", broken)
    with pytest.raises(SqlError) as error:
        SqlServerCatalog({"sales": config}).load(Principal("local", "analyst"), "sales")
    assert "NEVER_RETURN" not in str(error.value)


def test_sqlserver_config_and_policy_reload(tmp_path, driver):
    path = tmp_path / "connections.json"
    raw = {
        "connections": [
            {
                "id": "sales",
                "dialect": "sqlserver",
                "host": "db.example.test",
                "port": 1433,
                "dbname": "sales",
                "user": "reader",
                "passwordEnv": "ATLAS_TEST_SECRET",
                "sslmode": "verify-full",
                "grants": {"local#analyst": {"dbo.orders": ["id", "amount"]}},
            }
        ]
    }
    path.write_text(json.dumps(raw))
    adapter = DialectCatalog(path)
    first = adapter.load(Principal("local", "analyst"), "sales")
    raw["connections"][0]["grants"] = {"local#someone-else": {"dbo.orders": ["id"]}}
    path.write_text(json.dumps(raw))
    with pytest.raises(SqlError) as error:
        adapter.load(Principal("local", "analyst"), "sales")
    assert error.value.code == "CONNECTION_NOT_AUTHORIZED"
    raw["connections"][0]["sslmode"] = "require"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_connections(path)
    assert first.dialect == "sqlserver"


def test_dialect_and_endpoint_are_in_policy_hash(config):
    assert config.policy_version != replace(config, dialect="postgresql").policy_version
    assert config.policy_version != replace(config, host="another.example.test").policy_version


def test_missing_credentials(config, monkeypatch):
    monkeypatch.delenv("ATLAS_TEST_SECRET", raising=False)
    with pytest.raises(SqlError):
        odbc_string(config)


@pytest.mark.parametrize(
    "args,expected",
    [
        (("nvarchar", 160, 0, 0), "nvarchar(80)"),
        (("varchar", -1, 0, 0), "varchar(max)"),
        (("decimal", 9, 20, 2), "decimal(20,2)"),
        (("datetime2", 8, 0, 7), "datetime2(7)"),
    ],
)
def test_catalog_type_modifiers(args, expected):
    assert column_type(*args) == expected


def test_catalog_deadline_stops_subsequent_statements(monkeypatch):
    import atlas_sql_mcp.sqlserver_catalog as module

    current = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: current[0]))
    with module.catalog_deadline(Cursor(), 1.0) as check:
        check()
        current[0] = 2.0
        with pytest.raises(SqlError) as error:
            check()
        assert error.value.code == "VALIDATION_TIMEOUT"
        current[0] = 0.0
