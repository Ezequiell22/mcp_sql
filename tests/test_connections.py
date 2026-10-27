"""Connection parsing, ephemeral identity isolation, limits and secret redaction."""

import json
from dataclasses import replace

import psycopg
import pytest
from conftest import MemoryCatalog, args_for

from atlas_sql_mcp import connections
from atlas_sql_mcp.analyzer import PostgresAnalyzer
from atlas_sql_mcp.connections import SessionCatalog, discover, parse_connection
from atlas_sql_mcp.domain import Principal, SqlError
from atlas_sql_mcp.service import SqlService
from atlas_sql_mcp.sqlserver_catalog import odbc_string

URI = "postgresql://reader:synthetic-password@127.0.0.1:5433/demo"


@pytest.mark.parametrize(
    "value", [URI, "host=127.0.0.1 port=5433 dbname=demo user=reader password=synthetic-password"]
)
def test_postgres_uri_and_keyword_strings(value):
    config = parse_connection(value)
    assert (config.host, config.port, config.dbname, config.user) == (
        "127.0.0.1",
        5433,
        "demo",
        "reader",
    )
    assert config.sslmode == "disable"
    assert config.connect_kwargs()["password"] == "synthetic-password"
    assert "synthetic-password" not in repr(config)
    assert config.id.startswith("session-")
    assert "synthetic-password" not in config.policy_version


def test_percent_encoded_password_and_ipv6():
    config = parse_connection("postgresql://reader:p%40ss%3Aword%2F%7D@[::1]:5432/demo")
    assert config.password == "p@ss:word/}"
    assert config.host == "::1"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not a connection string",
        "postgresql://reader@127.0.0.1/demo",
        "postgresql://reader:secret@/demo",
        "service=hidden password=secret",
        URI + "?options=-c%20default_transaction_read_only=off",
        URI + "?passfile=/tmp/private",
        URI + "?sslrootcert=/tmp/private",
        URI + "?hostaddr=10.0.0.1",
        URI + "#secret",
        "host=/tmp dbname=demo user=reader password=secret",
        "host=127.0.0.1,example.test dbname=demo user=reader password=secret",
        "host=example.test port=0 dbname=demo user=reader password=secret",
        "host=example.test dbname=demo user=reader password=secret sslmode=disable",
        "host=127.0.0.1 dbname='postgresql://other/db' user=reader password=secret",
        "sqlserver://reader:secret@example.test/demo?sslmode=disable",
        "sqlserver://reader:secret@example.test/demo?sslmode=verify-full&sslmode=disable",
    ],
)
def test_invalid_or_policy_overriding_string_is_redacted(value):
    with pytest.raises(SqlError) as caught:
        parse_connection(value)
    assert caught.value.code == "INVALID_CONNECTION_STRING"
    assert "secret" not in str(caught.value)
    assert value not in str(caught.value) if value else True


def test_remote_tls_default_and_sqlserver_uri():
    pg = parse_connection("postgresql://reader:secret@example.test/demo")
    assert pg.sslmode == "verify-full"
    config = parse_connection("mssql://reader:secret%3B%7D@example.test/demo")
    assert config.port == 1433 and config.dialect == "sqlserver"
    assert config.sslmode == "verify-full"
    assert "PWD={secret;}}}" in odbc_string(config)


@pytest.fixture
def session_reader(monkeypatch, catalog):
    reads = []

    class Adapter:
        def __init__(self, configs):
            self.configs = configs

        def load(self, principal, key):
            config = self.configs[key]
            config.authorized(principal)
            reads.append(config)
            return replace(catalog, connection_id=key, policy_version=config.policy_version)

    monkeypatch.setattr(connections, "PostgresCatalog", Adapter)
    monkeypatch.setattr(
        connections, "discover", lambda config, schemas: [("public", "orders", "id")]
    )
    reader = SessionCatalog(MemoryCatalog(catalog))
    return reader, reads


def test_connect_context_finalize_uses_caller_owned_session(session_reader, principal):
    reader, reads = session_reader
    service = SqlService(reader, PostgresAnalyzer())
    connected = service.call("atlas_sql_connect", {"connectionString": URI}, principal)
    assert connected["status"] == "connected", connected
    assert "synthetic-password" not in json.dumps(connected)
    key = connected["connectionId"]
    ctx = service.call(
        "atlas_sql_context_get",
        {
            "connectionId": key,
            "contractVersion": "1.0",
            "request": "List order IDs",
        },
        principal,
    )
    assert ctx["status"] == "context", ctx
    result = service.call(
        "atlas_sql_finalize",
        args_for("SELECT id FROM public.orders", ["id"], ctx["contextId"]),
        principal,
    )
    assert result["status"] == "validated", result
    assert reads[0].grants == {principal.key: {"public.orders": ("id",)}}
    assert len(reads) == 4  # Initial validation, context, two freshness reads.
    assert result["artifact"]["validation"]["executionPerformed"] is False


def test_sessions_cannot_cross_principals(session_reader, principal):
    reader, reads = session_reader
    result = reader.connect({"connectionString": URI}, principal)
    with pytest.raises(SqlError) as caught:
        reader.load(Principal("other", principal.subject), result["connectionId"])
    assert caught.value.code == "CONNECTION_NOT_AUTHORIZED"
    assert len(reads) == 1


def test_expiry_drops_credentials(session_reader, principal):
    reader, _ = session_reader
    now = [1000]
    reader.clock = lambda: now[0]
    result = reader.connect({"connectionString": URI}, principal)
    now[0] += 3601
    with pytest.raises(SqlError):
        reader.load(principal, result["connectionId"])
    assert not reader._sessions


def test_shared_mode_disables_arbitrary_network_access(session_reader, principal):
    reader, reads = session_reader
    reader.allowed_endpoints = set()
    with pytest.raises(SqlError) as caught:
        reader.connect({"connectionString": URI}, principal)
    assert caught.value.code == "CONNECTION_DISABLED"
    reader.allowed_endpoints = {"other.example.test:5432"}
    with pytest.raises(SqlError) as caught:
        reader.connect({"connectionString": URI}, principal)
    assert caught.value.code == "CONNECTION_NOT_AUTHORIZED"
    assert not reads
    reader.allowed_endpoints = {"127.0.0.1:5433"}
    assert reader.connect({"connectionString": URI}, principal)["status"] == "connected"


def test_session_limit_enforced_before_opening_database(session_reader, principal):
    reader, reads = session_reader
    for _ in range(10):
        reader.connect({"connectionString": URI}, principal)
    with pytest.raises(SqlError) as caught:
        reader.connect({"connectionString": URI}, principal)
    assert caught.value.code == "RATE_LIMITED"
    assert len(reads) == 10


@pytest.mark.parametrize(
    "rows,code",
    [
        ([], "OBJECT_NOT_AUTHORIZED"),
        ([("public", "orders", "id")] * 10001, "INPUT_TOO_LARGE"),
        ([("public", "bad.name", "id")], "UNSUPPORTED_SQL_FEATURE"),
    ],
)
def test_discovery_failure_never_registers_connection(
    session_reader, principal, monkeypatch, rows, code
):
    reader, reads = session_reader
    monkeypatch.setattr(connections, "discover", lambda *args: rows)
    with pytest.raises(SqlError) as caught:
        reader.connect({"connectionString": URI}, principal)
    assert caught.value.code == code
    assert not reader._sessions and not reads


def test_driver_error_never_echoes_password(monkeypatch, catalog, principal):
    def fail(**kwargs):
        raise psycopg.OperationalError("raw synthetic-password host=private")

    monkeypatch.setattr(connections.psycopg, "connect", fail)
    reader = SessionCatalog(MemoryCatalog(catalog))
    result = SqlService(reader, PostgresAnalyzer()).call(
        "atlas_sql_connect", {"connectionString": URI}, principal
    )
    assert result["error"]["code"] == "CONNECTION_UNAVAILABLE"
    assert "synthetic-password" not in json.dumps(result)
    assert "private" not in json.dumps(result)
    assert not reader._sessions


def test_discovery_sqlserver_uses_escaped_credentials_and_only_metadata(monkeypatch):
    import sys
    from types import SimpleNamespace

    statements = []

    class Cursor:
        def execute(self, sql, params=()):
            statements.append((sql, params))

        def fetchone(self):
            return (16, 0, 0, 0)

        def fetchall(self):
            return [("dbo", "orders", "id")]

        def cancel(self):
            pass

    class DriverConnection:
        closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    db = DriverConnection()
    captured = []

    def connect(value, **kwargs):
        captured.append(value)
        return db

    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(connect=connect, Error=RuntimeError))
    config = parse_connection("sqlserver://reader:synthetic-password@example.test/demo")
    assert discover(config, ["dbo"]) == [("dbo", "orders", "id")]
    assert "PWD={synthetic-password}" in captured[0]
    assert len(statements) == 2
    assert "sys.tables" in statements[1][0]
    assert statements[1][1] == ("dbo",)
    assert db.closed


def test_generation_receives_metadata_without_connection_credentials(session_reader, principal):
    import asyncio

    pytest.importorskip("langchain_openai")
    from test_generation import PLAN, Model, proposal

    from atlas_sql_mcp.generation import SqlGenerator

    reader, _ = session_reader
    service = SqlService(reader, PostgresAnalyzer())
    connected = service.call("atlas_sql_connect", {"connectionString": URI}, principal)
    model = Model(PLAN, proposal())

    async def run():
        return await SqlGenerator(service, model).generate(
            {
                "contractVersion": "1.0",
                "connectionId": connected["connectionId"],
                "request": "List order IDs",
            },
            principal,
        )

    result = asyncio.run(run())
    assert result["status"] == "validated", result
    for messages in model.prompts:
        for message in messages:
            assert URI not in message.content
            assert "synthetic-password" not in message.content
    assert "synthetic-password" not in json.dumps(result)
