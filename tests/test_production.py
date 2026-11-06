"""Production boundary regressions; generated SQL is never executed."""

import json
import multiprocessing
import os
import time

import pytest
from langchain_core.runnables import RunnableLambda
from sqlalchemy import Column, Integer, MetaData, Table, event

from MCP import generator as module
from MCP import server
from MCP.generator import GenerationError, Generator, ScopeDecision, validate_sql
from MCP.settings import Settings
from tests.test_mcp import Model, scoped_database, settings


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT main.side_effect(id) FROM main.orders", "postgres"),
    ("SELECT pg_sleep(30) FROM main.orders", "postgres"),
    ("SELECT nextval('sequence') FROM main.orders", "postgres"),
    ("SELECT set_config('search_path', 'public', false) FROM main.orders", "postgres"),
    ("SELECT pg_advisory_lock(id) FROM main.orders", "postgres"),
    ("SELECT dblink('secret', 'SELECT 1') FROM main.orders", "postgres"),
    ("SELECT sleep(30) FROM main.orders", "mysql"),
    ("SELECT benchmark(100000, id) FROM main.orders", "mysql"),
    ("SELECT load_file('/etc/passwd') FROM main.orders", "mysql"),
    ("SELECT NEXT VALUE FOR main.seq FROM main.orders", "tsql"),
    ("SELECT main.side_effect(id) FROM main.orders", "tsql"),
    ("SELECT id FROM main.orders WITH (NOLOCK)", "tsql"),
    ("SELECT id FROM main.orders OPTION (RECOMPILE)", "tsql"),
    ("SELECT id FROM main.orders OPTION (MAXDOP 8)", "tsql"),
    ("SELECT id FROM main.orders /*!50000 INTO OUTFILE '/tmp/x' */", "mysql"),
    ("SELECT id FROM main.orders /*M! INTO OUTFILE '/tmp/x' */", "mysql"),
    ("SELECT /*+ IndexScan(orders) */ id FROM main.orders", "postgres"),
    ("SELECT id FROM main.orders WHERE", "postgres"),
    ("SELECT id FROM main.orders ignored garbage", "postgres"),
])
def test_guard_fails_closed_on_effects_hints_and_malformed_sql(sql, dialect):
    metadata = MetaData()
    Table("orders", metadata, Column("id", Integer), schema="main")
    with pytest.raises(GenerationError):
        validate_sql(sql, dialect, metadata, "main")


@pytest.mark.parametrize("dialect,expression", [
    ("postgres", "SUM(id), DATE_TRUNC('month', CURRENT_TIMESTAMP)"),
    ("tsql", "SUM(id), DATEADD(day, 1, GETDATE())"),
    ("mysql", "SUM(id), DATE_FORMAT(CURRENT_TIMESTAMP, '%Y-%m')"),
    ("sqlite", "SUM(id), strftime('%Y', 'now')"),
])
def test_ordinary_read_functions_remain_supported(dialect, expression):
    metadata = MetaData()
    Table("orders", metadata, Column("id", Integer), schema="main")
    sql = f"SELECT {expression} FROM main.orders"
    assert validate_sql(sql, dialect, metadata, "main") == sql


class MultiSchemaModel(Model):
    def with_structured_output(self, schema, **kwargs):
        if schema is ScopeDecision:
            return RunnableLambda(lambda messages: ScopeDecision(outcome="selected", names=["erp", "archive"]) if "compar" in json.loads(messages[-1][1])["request"].casefold() else ScopeDecision(outcome="selected", names=["erp"]))
        if schema is module.Selection:
            def choose(messages):
                candidates = json.loads(messages[-1][1])["candidates"]
                return module.Selection(names=[name for name in candidates if name.rsplit(".", 1)[-1] in {
                    "orders", "customers", "payments", "receipts", "order_report", "id", "amount", "order_id",
                }])
            return RunnableLambda(choose)
        return super().with_structured_output(schema, **kwargs)


def test_multi_schema_join_and_views_use_fresh_real_metadata(tmp_path):
    engine = scoped_database(tmp_path, ["erp", "archive"])
    # Only local fixture setup creates a view; production never creates objects.
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE VIEW archive.order_report AS SELECT id, amount FROM orders")
    sql = "SELECT a.id, r.amount FROM erp.orders a JOIN archive.order_report r ON r.id = a.id"
    model = MultiSchemaModel(responses=[sql, "SELECT id FROM erp.orders"])
    generator = Generator(settings(), engine=engine, model=model)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, s, p, ctx, many: statements.append(s))
    try:
        result = generator.generate("Compare current orders with archived reports by their matching order IDs")
        assert result["schemas"] == ["archive", "erp"] and result["schema"] is None
        assert {"erp.orders", "archive.order_report"} <= set(result["tables"])
        assert result["sql"] == sql
        assert "CREATE TABLE archive.order_report" in model.prompts[0]
        assert not any(sql == statement or "FROM archive.order_report" in statement for statement in statements)
        generator.generate("Current order IDs")
        assert "archive.order_report" not in model.prompts[-1]
        assert "archived reports" not in model.prompts[-1]
        generator.model = Model(responses=[sql])
        with pytest.raises(GenerationError, match="schema checks"):
            generator.generate("Only current IDs")
    finally:
        engine.dispose()


def test_view_only_schema_is_discovered(tmp_path, monkeypatch):
    engine = scoped_database(tmp_path, ["erp", "reports"])
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE VIEW reports.order_report AS SELECT id FROM orders")
    real_inspector = module.inspect(engine)

    class ViewOnlyReader:
        def get_table_names(self, schema=None, **kwargs):
            return [] if schema == "reports" else real_inspector.get_table_names(schema=schema, **kwargs)

        def __getattr__(self, name):
            return getattr(real_inspector, name)

    monkeypatch.setattr(module, "inspect", lambda _: ViewOnlyReader())
    try:
        sql = "SELECT id FROM reports.order_report"
        result = Generator(settings(), engine=engine, model=Model(responses=[sql])).generate(
            "Report IDs")
        assert result["sql"] == sql
    finally:
        engine.dispose()


def stalled_worker(connection, *args):
    time.sleep(30)


def crashed_worker(connection, *args):
    os._exit(1)


def partial_reply_worker(connection, *args):
    import struct
    os.write(connection.fileno(), struct.pack("!i", 65536) + b"x")
    time.sleep(30)


@pytest.mark.parametrize("target,expected", [(stalled_worker, "took too long"), (crashed_worker, "worker stopped"), (partial_reply_worker, "took too long")])
def test_isolated_worker_is_reaped_and_admission_recovers(tmp_path, monkeypatch, target, expected):
    engine = scoped_database(tmp_path, ["erp"])
    generator = Generator(settings(), engine=engine, model=Model(responses=["unused"]))
    generator.isolate = True
    monkeypatch.setattr(module, "generation_worker", target)
    monkeypatch.setattr(module, "REQUEST_TIMEOUT", 1.5)
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    try:
        with pytest.raises(GenerationError, match=expected):
            generator.generate("Order IDs")
        assert time.monotonic() - started < 4
        assert {child.pid for child in multiprocessing.active_children()} <= before
        # A timed-out/crashed worker cannot retain the admission lock or context.
        generator.isolate = False
        generator.model = Model(responses=["SELECT id FROM erp.orders"])
        assert generator.generate("Current order IDs")["status"] == "generated"
    finally:
        engine.dispose()


def test_busy_request_is_rejected_without_another_worker(tmp_path):
    engine = scoped_database(tmp_path, ["erp"])
    generator = Generator(settings(), engine=engine, model=Model(responses=["unused"]))
    try:
        with generator.lock:
            with pytest.raises(GenerationError, match="busy"):
                generator.generate("Order IDs")
    finally:
        engine.dispose()


def test_health_requires_fresh_live_event_loop_heartbeat(tmp_path, monkeypatch):
    marker = tmp_path / "health.json"
    monkeypatch.setattr(server, "HEALTH_FILE", marker)
    assert server.healthcheck() == 1
    for heartbeat, expected in [(time.monotonic(), 0), (time.monotonic() - 10, 1), (time.monotonic() + 10, 1)]:
        marker.write_text(json.dumps({"pid": os.getpid(), "heartbeat": heartbeat}))
        assert server.healthcheck() == expected
    marker.write_text('{"partial":')
    assert server.healthcheck() == 1
    marker.write_text(json.dumps({"pid": 999999999, "heartbeat": time.monotonic()}))
    assert server.healthcheck() == 1


@pytest.mark.parametrize("query", ["ssl_disabled=true", "ssl_verify_identity=false", "read_default_file=/tmp/options", "unix_socket=/tmp/mysql.sock", "init_command=DROP+TABLE+orders", "ssl_ca=a&ssl_ca=b"])
def test_mysql_url_cannot_override_application_connection_policy(monkeypatch, query):
    monkeypatch.setattr("MCP.settings.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://reader:private@db.example/test?" + query)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline")
    with pytest.raises(ValueError, match="verified TLS"):
        Settings.load()


@pytest.mark.parametrize("dialect,default", [("postgres", "public"), ("tsql", "dbo"), ("mysql", "erp"), ("sqlite", "main")])
def test_missing_physical_schema_is_filled_from_verified_default(dialect, default):
    import sqlglot
    from sqlglot import exp
    metadata = MetaData()
    Table("orders", metadata, Column("id", Integer), schema=default)
    sql = "WITH chosen AS (SELECT id FROM orders) SELECT id FROM chosen"
    result = validate_sql(sql, dialect, metadata, default, default_schema=default)
    tables = list(sqlglot.parse_one(result, read=dialect).find_all(exp.Table))
    assert next(table for table in tables if table.name == "orders").db == default
    assert next(table for table in tables if table.name == "chosen").db == ""


def test_default_never_authorizes_an_uninspected_schema_or_table():
    metadata = MetaData()
    Table("orders", metadata, Column("id", Integer), schema="erp")
    with pytest.raises(GenerationError):
        validate_sql("SELECT id FROM orders", "postgres", metadata, "erp", default_schema="public")
    with pytest.raises(GenerationError):
        validate_sql("SELECT id FROM missing", "postgres", metadata, "erp", default_schema="erp")


def test_executable_comment_marker_inside_literal_is_ordinary_data():
    metadata = MetaData()
    Table("orders", metadata, Column("id", Integer), schema="main")
    sql = "SELECT id, '/*!example*/' AS label FROM main.orders"
    assert validate_sql(sql, "mysql", metadata, "main") == sql


def test_more_than_twenty_tables_can_be_selected_from_metadata(tmp_path):
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{tmp_path / 'many.db'}")
    metadata = MetaData()
    for index in range(25):
        Table(f"t{index}", metadata, Column("id", Integer))
    metadata.create_all(engine)
    sql = "SELECT t0.id FROM main.t0 t0 " + " ".join(f"JOIN main.t{i} t{i} ON t{i}.id = t0.id" for i in range(1, 25))
    try:
        result = Generator(settings(), engine=engine, model=Model(responses=[sql])).generate(
            "Find matching IDs across all related records")
        assert len(result["tables"]) == 25 and result["sql"] == sql
    finally:
        engine.dispose()
