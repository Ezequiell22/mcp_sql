"""Connection parsing, ephemeral identity isolation, limits and secret redaction."""

import json
from dataclasses import replace

import psycopg
import pytest
from conftest import MemoryCatalog, args_for

from atlas_sql_mcp.analysis.postgres import PostgresAnalyzer
from atlas_sql_mcp.application import connections
from atlas_sql_mcp.application.connections import SessionCatalog, discover, parse_connection
from atlas_sql_mcp.application.service import SqlService
from atlas_sql_mcp.domain import Principal, SqlError
from atlas_sql_mcp.metadata.sqlserver import odbc_string

URI = "postgresql://reader:synthetic-password@127.0.0.1:5433/demo"


@pytest.mark.parametrize(
    "value",
    [
        URI,
        " \n" + URI + "\n ",
        "host=127.0.0.1 port=5433 dbname=demo user=reader password=synthetic-password",
    ],
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
        "sqlserver://reader:secret@example.test:0/demo",
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

        def load(self, principal, key, relations=None):
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
    assert len(reads) == 3  # Context metadata and two freshness reads; connect reads names only.
    assert result["artifact"]["validation"]["executionPerformed"] is False


def test_sessions_cannot_cross_principals(session_reader, principal):
    reader, reads = session_reader
    result = reader.connect({"connectionString": URI}, principal)
    with pytest.raises(SqlError) as caught:
        reader.load(Principal("other", principal.subject), result["connectionId"])
    assert caught.value.code == "CONNECTION_NOT_AUTHORIZED"
    assert len(reads) == 0


def test_expiry_drops_credentials(session_reader, principal):
    reader, _ = session_reader
    now = [1000]
    reader.clock = lambda: now[0]
    result = reader.connect({"connectionString": URI}, principal)
    now[0] += 3601
    with pytest.raises(SqlError) as caught:
        reader.load(principal, result["connectionId"])
    assert caught.value.code == "CONNECTION_EXPIRED"
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
    assert not reads  # Connecting never materializes every table.


@pytest.mark.parametrize(
    "rows,code",
    [
        ([], "OBJECT_NOT_AUTHORIZED"),
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

    monkeypatch.setattr(
        __import__("atlas_sql_mcp.metadata.discovery", fromlist=["psycopg"]).psycopg,
        "connect",
        fail,
    )
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
    discovered = discover(config, ["dbo"])
    assert discovered == [("dbo", "orders", "id")]
    assert discovered.database_major == 16
    assert "PWD={synthetic-password}" in captured[0]
    assert len(statements) == 2
    assert "sys.tables" in statements[1][0]
    assert statements[1][1] == ("dbo",)
    assert db.closed


def test_agent_context_excludes_connection_credentials(session_reader, principal):
    reader, _ = session_reader
    service = SqlService(reader, PostgresAnalyzer())
    connected = service.call("atlas_sql_connect", {"connectionString": URI}, principal)
    context = service.call(
        "atlas_sql_context_get",
        {
            "contractVersion": "1.0",
            "connectionId": connected["connectionId"],
            "request": "List order IDs",
        },
        principal,
    )
    assert context["status"] == "context", context
    assert URI not in json.dumps(context)
    assert "synthetic-password" not in json.dumps(context)


@pytest.mark.parametrize(
    "value",
    [
        "Server=tcp:sql.example.test,1444;Database=demo;UID=reader;PWD={p;ass}}word};",
        "Driver={ODBC Driver 18 for SQL Server};Server=sql.example.test,1444;"
        "Database=demo;UID=reader;PWD={p;ass}}word};Encrypt=yes;TrustServerCertificate=no",
        " Data Source=sql.example.test,1444; Initial Catalog=demo; User ID=reader;"
        " Password={p;ass}}word}; Encrypt=True; Trust Server Certificate=False; ",
    ],
)
def test_sqlserver_keyword_strings(value):
    config = parse_connection(value)
    assert config.dialect == "sqlserver"
    assert (config.host, config.port, config.dbname, config.user) == (
        "sql.example.test",
        1444,
        "demo",
        "reader",
    )
    assert config.password == "p;ass}word"
    assert config.sslmode == "verify-full"
    assert "PWD={p;ass}}word}" in odbc_string(config)
    assert config.password not in repr(config)


def test_sqlserver_keyword_default_port_and_ipv6():
    assert parse_connection("Server=sql.example.test;Database=db;UID=u;PWD=p").port == 1433
    config = parse_connection("Server=tcp:[::1],1434;Database=db;UID=u;PWD=p")
    assert config.host == "::1" and config.port == 1434


@pytest.mark.parametrize(
    "suffix",
    [
        "Encrypt=no",
        "Encrypt=optional",
        "Encrypt=strict",
        "TrustServerCertificate=yes",
        "Trust Server Certificate=true",
        "Driver=/tmp/driver",
        "Driver={ODBC Driver 17 for SQL Server}",
        "DSN=private",
        "Trusted_Connection=yes",
        "Authentication=ActiveDirectoryPassword",
        "AttachDBFileName=/tmp/file",
        "APP=other",
        "UID=other",
        "User ID=other",
        "Server=other",
        "Data Source=other",
        "PWD={unterminated",
        "PWD={p}junk",
        "Unknown=secret",
    ],
)
def test_sqlserver_keyword_options_fail_closed(suffix):
    value = "Server=sql.example.test;Database=db;UID=u;PWD=secret;" + suffix
    with pytest.raises(SqlError) as caught:
        parse_connection(value)
    assert caught.value.code == "INVALID_CONNECTION_STRING"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "server", ["host,0", "host,65536", r"host\instance", "np:host", "host,1,2"]
)
def test_sqlserver_keyword_invalid_endpoint(server):
    with pytest.raises(SqlError):
        parse_connection(f"Server={server};Database=db;UID=u;PWD=secret")


def test_sqlserver_keyword_session_uses_sqlserver_adapter(monkeypatch, catalog, principal):
    reads = []

    class Adapter:
        def __init__(self, configs):
            self.configs = configs

        def load(self, owner, key, relations=None):
            config = self.configs[key]
            config.authorized(owner)
            reads.append(config)
            return replace(
                catalog,
                connection_id=key,
                policy_version=config.policy_version,
                dialect="sqlserver",
                database_major=16,
            )

    monkeypatch.setattr(connections, "SqlServerCatalog", Adapter)
    monkeypatch.setattr(connections, "discover", lambda *args: [("dbo", "orders", "id")])
    reader = SessionCatalog(MemoryCatalog(catalog))
    result = reader.connect(
        {"connectionString": "Server=sql.example.test;Database=db;UID=u;PWD=secret"}, principal
    )
    assert result["dialect"] == "sqlserver" and result["contractVersion"] == "1.1"
    assert "secret" not in json.dumps(result)
    assert reader.load(principal, result["connectionId"]).dialect == "sqlserver"
    assert len(reads) == 1


def test_standalone_connections_need_no_configured_catalog(session_reader, principal):
    reader, _ = session_reader
    reader.configured = None
    connected = reader.connect({"connectionString": URI}, principal)
    assert (
        reader.load(principal, connected["connectionId"]).connection_id == connected["connectionId"]
    )
    with pytest.raises(SqlError) as caught:
        reader.load(principal, "sample-database")
    assert caught.value.code == "CONNECTION_NOT_AUTHORIZED"


def test_expired_session_never_falls_back_to_configured_catalog(session_reader, principal):
    reader, _ = session_reader

    class ForbiddenFallback:
        def load(self, *args):
            pytest.fail("A missing session must never resolve to preconfigured metadata")

    reader.configured = ForbiddenFallback()
    with pytest.raises(SqlError) as caught:
        reader.load(principal, "session-not-present")
    assert caught.value.code == "CONNECTION_EXPIRED"


def test_concurrent_connect_reserves_capacity_before_database_io(
    session_reader, principal, monkeypatch
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    reader, reads = session_reader
    entered = threading.Barrier(11)
    release = threading.Event()

    def blocked_discovery(*args):
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return [("public", "orders", "id")]

    monkeypatch.setattr(connections, "discover", blocked_discovery)
    with ThreadPoolExecutor(max_workers=10) as pool:
        pending = [
            pool.submit(reader.connect, {"connectionString": URI}, principal) for _ in range(10)
        ]
        try:
            entered.wait(timeout=5)
            with pytest.raises(SqlError) as caught:
                reader.connect({"connectionString": URI}, principal)
            assert caught.value.code == "RATE_LIMITED"
            assert not reads
        finally:
            release.set()
        assert all(f.result()["status"] == "connected" for f in pending)
    assert len(reader._sessions) == 10
    assert not reader._pending


def test_failed_connection_releases_capacity(session_reader, principal):
    reader, _ = session_reader
    for _ in range(12):
        with pytest.raises(SqlError):
            reader.connect({"connectionString": "invalid"}, principal)
    assert not reader._pending
    assert reader.connect({"connectionString": URI}, principal)["status"] == "connected"


def test_shutdown_drops_credentials_and_rejects_late_connect(
    session_reader, principal, monkeypatch
):
    reader, _ = session_reader
    reader.connect({"connectionString": URI}, principal)

    def closing_discovery(*args):
        reader.close()
        return [("public", "orders", "id")]

    monkeypatch.setattr(connections, "discover", closing_discovery)
    with pytest.raises(SqlError) as caught:
        reader.connect({"connectionString": URI}, principal)
    assert caught.value.code == "CONNECTION_UNAVAILABLE"
    assert not reader._sessions and not reader._pending
    with pytest.raises(SqlError):
        reader.connect({"connectionString": URI}, principal)
    with pytest.raises(SqlError) as caught:
        reader.load(principal, "configured-database")
    assert caught.value.code == "CONNECTION_UNAVAILABLE"


@pytest.mark.parametrize("field", ["host", "user", "password", "dbname", "port"])
def test_sqlserver_uri_query_cannot_override_authority_credentials(field):
    with pytest.raises(SqlError) as caught:
        parse_connection(f"sqlserver://reader:secret@example.test/demo?{field}=other")
    assert caught.value.code == "INVALID_CONNECTION_STRING"


def test_large_schema_connects_without_loading_full_metadata(
    session_reader, principal, monkeypatch
):
    reader, reads = session_reader
    rows = [("public", f"table_{i}", f"column_{j}") for i in range(1500) for j in range(10)]
    monkeypatch.setattr(connections, "discover", lambda *args: rows)
    connected = reader.connect({"connectionString": URI}, principal)
    assert connected["relationCount"] == 1500
    assert not reads
    index = reader.index(principal, connected["connectionId"])
    assert len(index.relations) == 1500
    assert index.relations[0].columns[0].sql_type == ""
    assert not reads


def test_session_index_rechecks_database_permissions(session_reader, principal, monkeypatch):
    reader, _ = session_reader
    connected = reader.connect({"connectionString": URI}, principal)
    before = reader.index(principal, connected["connectionId"])
    monkeypatch.setattr(connections, "discover", lambda *args: [])
    after = reader.index(principal, connected["connectionId"])
    assert before.relations and not after.relations
    assert before.fingerprint != after.fingerprint


def test_sqlserver_lightweight_index_keeps_verified_version(monkeypatch, principal):
    from atlas_sql_mcp.metadata.discovery import DiscoveryRows

    monkeypatch.setattr(
        connections, "discover", lambda *args: DiscoveryRows([("dbo", "orders", "id")], 17)
    )
    reader = SessionCatalog()
    connected = reader.connect(
        {"connectionString": "sqlserver://reader:synthetic-password@example.test/demo"},
        principal,
    )
    index = reader.index(principal, connected["connectionId"])
    assert index.database_major == 17
    assert index.dialect == "sqlserver"
    assert [relation.key for relation in index.relations] == ["dbo.orders"]


def test_metadata_streaming_stops_at_byte_budget(monkeypatch):
    from atlas_sql_mcp.metadata import discovery

    class Cursor:
        def __init__(self):
            self.calls = 0

        def fetchmany(self, size):
            assert size == 512
            self.calls += 1
            return [("public", "orders", "id")]

        def fetchall(self):
            pytest.fail("Discovery must consume bounded batches")

    cursor = Cursor()
    monkeypatch.setattr(discovery, "MAX_DISCOVERY_BYTES", 100)
    with pytest.raises(SqlError) as error:
        discovery.read_names(cursor, 18)
    assert error.value.code == "METADATA_CAPACITY_EXCEEDED"
    assert cursor.calls == 2


def test_session_metadata_admission_releases_expired_storage(
    session_reader, principal, monkeypatch
):
    reader, _ = session_reader
    now = [1000]
    reader.clock = lambda: now[0]
    first = reader.connect({"connectionString": URI}, principal)
    cost = reader._sessions[first["connectionId"]].storage_bytes
    monkeypatch.setattr(connections, "MAX_RETAINED_METADATA_BYTES", cost)
    with pytest.raises(SqlError) as error:
        reader.connect({"connectionString": URI}, principal)
    assert error.value.code == "METADATA_CAPACITY_EXCEEDED"
    assert error.value.retryable
    assert len(reader._sessions) == 1
    now[0] += 3601
    assert reader.connect({"connectionString": URI}, principal)["status"] == "connected"
    assert len(reader._sessions) == 1


def test_oversized_connection_metadata_never_registers(session_reader, principal, monkeypatch):
    reader, _ = session_reader
    monkeypatch.setattr(connections, "MAX_SESSION_METADATA_BYTES", 1)
    with pytest.raises(SqlError) as error:
        reader.connect({"connectionString": URI}, principal)
    assert error.value.code == "METADATA_CAPACITY_EXCEEDED"
    assert not reader._sessions
