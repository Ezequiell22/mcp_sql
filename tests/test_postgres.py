"""Disposable PostgreSQL integration, optionally using the real DeepSeek API over MCP."""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
import sqlglot
from dotenv import dotenv_values
from mcp.client.stdio import stdio_client
from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from mcp import ClientSession, StdioServerParameters
from MCP.generator import GenerationError, Generator
from MCP.settings import Settings
from tests.test_mcp import Model

pytestmark = pytest.mark.skipif(
    os.getenv("ALLOW_SYNTHETIC_DB_WRITES") != "1", reason="Disposable database gate is not enabled"
)


@pytest.fixture(scope="module", params=["postgres:18", "postgres:17-alpine"], ids=["pg18", "pg17"])
def postgres(request):
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            "atlas-langchain-test-" + uuid.uuid4().hex[:12],
            "-e",
            "POSTGRES_PASSWORD=disposable-test-only",
            "-p",
            "127.0.0.1::5432",
            request.param,
        ],
        text=True,
    ).strip()
    try:
        address = subprocess.check_output(
            ["docker", "port", container, "5432/tcp"], text=True
        ).strip()
        port = int(address.rsplit(":", 1)[1])
        admin = dict(
            host="127.0.0.1",
            port=port,
            dbname="postgres",
            user="postgres",
            password="disposable-test-only",
            connect_timeout=1,
        )
        deadline = time.monotonic() + 40
        while True:
            try:
                connection = psycopg.connect(**admin, autocommit=True)
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
        with connection:
            connection.execute("CREATE ROLE reader LOGIN PASSWORD 'disposable-reader-only'")
            connection.execute("CREATE ROLE scoped_reader LOGIN PASSWORD 'disposable-scoped-only'")
            connection.execute("ALTER ROLE scoped_reader SET search_path = erp, public")
            connection.execute(
                psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    psycopg.sql.Identifier("report+reader"),
                    psycopg.sql.Literal("fixture:p@ss/word?#%+"),
                )
            )
            for schema in ["erp", "archive"]:
                connection.execute(
                    psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema))
                )
                connection.execute(
                    psycopg.sql.SQL("CREATE TABLE {}.customers(id integer PRIMARY KEY)").format(
                        psycopg.sql.Identifier(schema)
                    )
                )
                connection.execute(
                    psycopg.sql.SQL(
                        "CREATE TABLE {}.orders(id integer PRIMARY KEY, customer_id integer REFERENCES {}.customers(id), amount numeric)"
                    ).format(psycopg.sql.Identifier(schema), psycopg.sql.Identifier(schema))
                )
                connection.execute(
                    psycopg.sql.SQL(
                        "CREATE TABLE {}.payments(id integer PRIMARY KEY, customer_id integer REFERENCES {}.customers(id), amount numeric)"
                    ).format(psycopg.sql.Identifier(schema), psycopg.sql.Identifier(schema))
                )
                connection.execute(
                    psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO reader").format(
                        psycopg.sql.Identifier(schema)
                    )
                )
            # Enough tables to require actual model selection for automatic erp discovery.
            for index in range(25):
                connection.execute(
                    psycopg.sql.SQL("CREATE TABLE erp.{}(id integer)").format(
                        psycopg.sql.Identifier(f"irrelevant_{index}")
                    )
                )
            connection.execute(
                "CREATE TABLE erp.wide(id integer PRIMARY KEY,"
                + ",".join(f"c{i} integer" for i in range(700))
                + ")"
            )
            connection.execute("CREATE TABLE public.anchor(id integer PRIMARY KEY)")
            connection.execute("CREATE VIEW archive.order_report AS SELECT id, amount FROM archive.orders")
            connection.execute("CREATE MATERIALIZED VIEW archive.order_totals AS SELECT customer_id, SUM(amount) AS total FROM archive.orders GROUP BY customer_id WITH NO DATA")
            connection.execute("CREATE TABLE archive.receipts(id integer PRIMARY KEY, order_id integer REFERENCES erp.orders(id))")
            connection.execute(
                "GRANT SELECT ON ALL TABLES IN SCHEMA erp, archive, public TO reader"
            )
            connection.execute('GRANT USAGE ON SCHEMA erp TO "report+reader"')
            connection.execute('GRANT SELECT ON ALL TABLES IN SCHEMA erp TO "report+reader"')
            connection.execute("GRANT USAGE ON SCHEMA erp TO scoped_reader")
            connection.execute("GRANT SELECT ON ALL TABLES IN SCHEMA erp TO scoped_reader")
        yield {
            "url": f"postgresql+psycopg://reader:disposable-reader-only@127.0.0.1:{port}/postgres",
            "scoped_url": f"postgresql+psycopg://scoped_reader:disposable-scoped-only@127.0.0.1:{port}/postgres",
            "container_url": "postgresql+psycopg://reader:disposable-reader-only@127.0.0.1:5432/postgres",
            "container": container,
            "urls": [
                URL.create(
                    "postgresql+psycopg",
                    username=username,
                    password=password,
                    host=host,
                    port=port,
                    database="postgres",
                    query=query,
                ).render_as_string(hide_password=False)
                for username, password, host, query in [
                    ("reader", "disposable-reader-only", "127.0.0.1", {}),
                    (
                        "reader",
                        "disposable-reader-only",
                        "localhost",
                        {"application_name": "MCP usage test"},
                    ),
                    ("report+reader", "fixture:p@ss/word?#%+", "127.0.0.1", {}),
                    ("report+reader", "fixture:p@ss/word?#%+", "localhost", {"sslmode": "disable"}),
                ]
            ],
        }
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=True, capture_output=True)


@pytest.mark.parametrize(
    "variant",
    range(4),
    ids=["ip", "hostname-query", "encoded-credentials", "hostname-encoded-credentials"],
)
def test_postgres_connection_url_variants(postgres, monkeypatch, variant):
    monkeypatch.setenv("DATABASE_URL", postgres["urls"][variant])
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-only")
    settings = Settings.load()
    sql = "SELECT id FROM erp.orders WHERE id > 10"
    generator = Generator(settings, model=Model(responses=[sql]))
    try:
        result = generator.generate("Order IDs above 10")
        assert result["sql"] == sql and not result["execution_performed"]
    finally:
        generator.engine.dispose()


@pytest.mark.parametrize("failure", ["password", "database", "endpoint", "tls"])
def test_postgres_connection_failures_are_sanitized(postgres, failure):
    from mcp.server.mcpserver.exceptions import ToolError

    from MCP.server import create_server

    url = make_url(postgres["url"])
    if failure == "password":
        url = url.set(password="invalid-disposable-password")
    elif failure == "database":
        url = url.set(database="unavailable_test_database")
    elif failure == "endpoint":
        url = url.set(port=9)
    else:
        url = url.update_query_dict({"sslmode": "verify-full"})
    server = create_server(Settings(url.render_as_string(hide_password=False), "offline-only"))
    with pytest.raises(ToolError) as error:
        asyncio.run(server.call_tool("generate_sql", {"question": "Order IDs"}))
    message = str(error.value)
    assert "Generation failed" in message
    assert "disposable" not in message and "postgresql" not in message


def test_request_schemas_are_independent_and_metadata_only(postgres):
    queries = [f"SELECT id FROM {schema}.orders" for schema in ["erp", "archive", "erp"]]
    generator = Generator(
        Settings(postgres["url"], "offline-test-only"), model=Model(responses=queries)
    )
    statements = []

    @event.listens_for(generator.engine, "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        for schema, sql in zip(["erp", "archive", "erp"], queries, strict=True):
            result = generator.generate("Archived order IDs" if schema == "archive" else "Current order IDs")
            assert result["sql"] == sql and result["schema"] == schema
            assert result["tables"] == [f"{schema}.orders"]
            assert result["execution_performed"] is False
        assert not any(
            "from erp.orders" in s.lower() or "from archive.orders" in s.lower() for s in statements
        )
        with generator.engine.connect() as connection:
            assert connection.exec_driver_sql("SHOW transaction_read_only").scalar() == "on"
        # Previous inspection of erp must not authorize an erp reference in archive.
        generator.model = Model(responses=["SELECT id FROM erp.orders"])
        with pytest.raises(GenerationError, match="schema checks"):
            generator.generate("Archived order IDs")
    finally:
        generator.engine.dispose()


def test_postgres_fills_native_and_custom_connection_defaults(postgres):
    for url, question, default, sql in [
        (postgres["url"], "Anchor IDs", "public", "SELECT id FROM anchor"),
        (postgres["scoped_url"], "Current order IDs", "erp", "SELECT id FROM orders"),
    ]:
        generator = Generator(Settings(url, "offline"), model=Model(responses=[sql]))
        try:
            result = generator.generate(question)
            assert result["default_schema"] == default
            assert sqlglot.parse_one(result["sql"], read="postgres").find(exp.Table).db == default
        finally:
            generator.engine.dispose()


def test_postgres_materialized_view_metadata(postgres):
    sql = "SELECT customer_id, total FROM archive.order_totals"
    generator = Generator(Settings(postgres["url"], "offline"), model=Model(responses=[sql]))
    statements = []
    event.listen(generator.engine, "before_cursor_execute", lambda c, cur, s, p, ctx, many: statements.append(s))
    try:
        result = generator.generate("Archived order totals by customer ID")
        assert result["sql"] == sql and result["tables"] == ["archive.order_totals"]
        assert not any("from archive.order_totals" in s.lower() for s in statements)
    finally:
        generator.engine.dispose()


def test_postgres_cross_schema_views_and_foreign_keys(postgres):
    from tests.test_production import MultiSchemaModel
    sql = "SELECT o.id, v.amount FROM erp.orders o JOIN archive.receipts r ON r.order_id = o.id JOIN archive.order_report v ON v.id = r.id"
    model = MultiSchemaModel(responses=[sql])
    generator = Generator(Settings(postgres["url"], "offline"), model=model)
    statements = []
    event.listen(generator.engine, "before_cursor_execute", lambda c, cur, s, p, ctx, many: statements.append(s))
    try:
        result = generator.generate("Compare current orders with archived receipts and reports, matching order_id to current order id and report id to receipt id")
        assert result["sql"] == sql and result["schemas"] == ["archive", "erp"]
        assert "REFERENCES erp.orders" in model.prompts[0]
        assert "CREATE TABLE archive.order_report" in model.prompts[0]
        assert not any("from erp.orders" in s.lower() or "from archive.order_report" in s.lower() for s in statements)
    finally:
        generator.engine.dispose()


@pytest.mark.parametrize("container", [False, True])
def test_live_deepseek_through_real_mcp(postgres, container):
    if os.getenv("RUN_LIVE_DEEPSEEK") != "1":
        pytest.skip("Set RUN_LIVE_DEEPSEEK=1 to authorize billed provider calls")
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / ".env")
    key = os.getenv("DEEPSEEK_API_KEY") or values.get("DEEPSEEK_API_KEY")
    if not key:
        pytest.fail("Live test requires DEEPSEEK_API_KEY; credentials are never printed")
    model = os.getenv("DEEPSEEK_MODEL") or values.get("DEEPSEEK_MODEL") or "deepseek-flash"
    environment = {
        "DEEPSEEK_API_KEY": key,
        "DEEPSEEK_MODEL": model,
        "DATABASE_URL": postgres["url"],
    }
    if container:
        image = os.getenv("MCP_TEST_IMAGE")
        if not image:
            pytest.fail("Live Docker test requires MCP_TEST_IMAGE")
        environment["DATABASE_URL"] = postgres["container_url"]
        command = "docker"
        args = [
            "run",
            "--rm",
            "-i",
            "--read-only",
            "--network",
            f"container:{postgres['container']}",
        ]
        for name in environment:
            args.extend(["--env", name])  # Values come from environment, never command arguments.
        args.append(image)
    else:
        command, args = sys.executable, ["-m", "MCP.server"]
    params = StdioServerParameters(command=command, args=args, env=environment, cwd=str(root))

    async def check():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                contract = tools.tools[0].input_schema["properties"]
                assert set(contract) == {"question"}
                cases = [
                    {
                        "question": "Total twice the order amount per customer ID. Join customers to orders on customer_id = customers.id. Include only orders with amount > 10. Group by customer ID. Return customer ID and SUM(orders.amount * 2).",
                        "schema": "erp",
                    },
                ]
                if not container:
                    cases.extend(
                        [
                            {
                                "question": "For archived records, sum order amount by customer ID, joining customers with orders on customer_id = customers.id, where amount > 20. Return customer ID and the sum, grouped by customer ID.",
                                "schema": "archive",
                            },
                            {
                                "question": "Return id and c699 from wide where c699 > 10.",
                                "schema": "erp",
                            },
                            {
                                "question": "Return id from anchor where id > 0.",
                                "schema": "public",
                            },
                            {
                                "question": "For every customer ID, return remaining balance: total orders.amount minus total payments.amount. Orders and payments each have multiple independent rows per customer. Preaggregate each fact separately by customer_id before joining to customers; include customers without either fact, treating absent totals as zero. Do not join raw orders to raw payments.",
                                "schema": "erp",
                            },
                            {
                                "question": "For archived records, return customer IDs that have no orders. Include customers without any activity. Do not invent status or date filters.",
                                "schema": "archive",
                            },
                        ]
                    )
                cases.extend([
                    {"question": "For current business records, what is the total order value for each customer?"},
                    {"question": "Which customers in the archived records have never placed an order?"},
                ])
                for index, arguments in enumerate(cases):
                    # Expected scope is test data, never a public tool parameter.
                    response = await session.call_tool("generate_sql", {"question": arguments["question"]})
                    assert not response.is_error, (
                        f"Live provider generation returned a sanitized MCP error at case {index}"
                    )
                    result = response.structured_content
                    assert result and result["execution_performed"] is False
                    schema = arguments.get("schema", "archive" if "archived" in arguments["question"] else "erp")
                    assert result["schema"] == schema
                    sql = result["sql"]
                    assert key not in sql and "disposable-reader-only" not in sql
                    tree = sqlglot.parse_one(sql, read="postgres")
                    for scope in traverse_scope(tree):
                        assert all(
                            source.db == schema
                            for source in scope.sources.values()
                            if isinstance(source, exp.Table)
                        )
                    if "schema" in arguments:
                        if index < 4:
                            assert tree.find(exp.Where) is not None
                        if index < 2:
                            assert tree.find(exp.Join) is not None and tree.find(exp.Group) is not None
                            assert tree.find(exp.Sum) is not None
                            if index == 0:
                                assert tree.find(exp.Mul) is not None
                        elif index == 2:
                            assert {column.name for column in tree.find_all(exp.Column)} >= {
                                "id",
                                "c699",
                            }
                        elif index == 4:
                            assert len(list(tree.find_all(exp.Group))) >= 2
                            assert len(list(tree.find_all(exp.Join))) >= 2
                            assert (
                                tree.find(exp.Coalesce) is not None and tree.find(exp.Sub) is not None
                            )
                        elif index == 5:
                            assert tree.find(exp.Exists) is not None or (
                                tree.find(exp.Join) is not None and tree.find(exp.Is) is not None
                            )
                    elif "archived" in arguments["question"]:
                        assert tree.find(exp.Exists) is not None or (
                            tree.find(exp.Join) is not None and tree.find(exp.Is) is not None
                        )
                    else:
                        assert tree.find(exp.Group) is not None and tree.find(exp.Sum) is not None
                    # Deliberately do not execute the generated SQL.

    asyncio.run(check())
