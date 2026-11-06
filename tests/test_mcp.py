"""Offline contract regressions: real LangChain generation, no generated query execution."""

import json
import re
import time

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event

from MCP import generator as module
from MCP.generator import GenerationError, Generator, Selection, ScopeDecision, ClarificationNeeded, validate_sql
from MCP.settings import Settings


class Model(FakeListChatModel):
    reason: str = "stop"
    prompts: list[str] = []
    selections: list[list[str]] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts.append("\n".join(str(m.content) for m in messages))
        result = super()._generate(messages, stop, run_manager, **kwargs)
        result.generations[0].message.response_metadata["finish_reason"] = self.reason
        return result

    def with_structured_output(self, schema, **kwargs):
        if schema is ScopeDecision:
            def choose_scope(messages):
                payload = json.loads(messages[-1][1])
                catalog = payload["catalog"]
                question = payload["request"].casefold()
                desired = "archive" if "archiv" in question else "reports" if "report ids" in question else "erp"
                if "anchor" in question:
                    desired = "public" if "public" in catalog else "dbo"
                return ScopeDecision(outcome="selected", names=[desired if desired in catalog else next(iter(catalog))])
            return RunnableLambda(choose_scope)
        assert schema is Selection

        def select(messages):
            candidates = json.loads(messages[-1][1])["candidates"]
            self.selections.append(candidates)
            if "table names" in messages[0][1]:
                sql = self.responses[self.i]
                referenced = {name.casefold() for name in re.findall(r"(?:FROM|JOIN)\s+(?:\w+\.)?(\w+)", sql, re.I)}
                return Selection(names=[name for name in candidates if name.rsplit(".", 1)[-1].casefold() in referenced] if referenced else candidates)
            # Stable business target and the key/value columns of the wide table.
            names = [n for n in candidates if n in {"target", "id", "c699"}]
            return Selection(names=names)

        return RunnableLambda(select)


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    metadata = MetaData()
    Table("customers", metadata, Column("id", Integer, primary_key=True))
    Table(
        "orders",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("customer_id", Integer),
        Column("amount", Integer),
    )
    metadata.create_all(engine)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    yield engine, metadata, statements
    engine.dispose()


def settings():
    return Settings("sqlite://", "offline-test-key")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c.id, SUM(o.amount * 2) AS total FROM main.customers c "
        "JOIN main.orders o ON o.customer_id = c.id WHERE o.amount > 10 GROUP BY c.id",
        "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM main.orders "
        "GROUP BY customer_id) SELECT c.id, t.total FROM main.customers c "
        "JOIN totals t ON t.customer_id = c.id",
        "SELECT customer_id, SUM(amount) OVER (PARTITION BY customer_id) AS total FROM main.orders",
    ],
)
def test_real_chain_preserves_sql_and_reads_metadata_only(database, sql):
    engine, _, statements = database
    model = Model(responses=[sql])
    result = Generator(settings(), engine=engine, model=model).generate(
        "Totals by customer, calculated as double the order amount, where amount exceeds 10")
    assert result["sql"] == sql
    assert result["execution_performed"] is False
    assert model.prompts and "CREATE TABLE" in model.prompts[0]
    assert "3 rows" not in model.prompts[0]
    # SQLite catalog SELECTs are expected; no application-row SELECT is allowed.
    assert not any(
        "from main.orders" in s.lower() or "from main.customers" in s.lower() for s in statements
    )
    assert sql not in statements


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE main.orders",
        "DELETE FROM main.orders",
        "SELECT id FROM main.orders; SELECT 2",
        "SELECT secret FROM main.orders",
        "SELECT id FROM main.missing",
        "SELECT 1 FROM main.missing",
        "SELECT COUNT(*) FROM main.missing",
        "SELECT id FROM main.orders WHERE EXISTS (SELECT 1 FROM main.missing)",
        "SELECT id INTO copy FROM main.orders",
        "SELECT * FROM main.orders",
        "SELECT orders.* FROM main.orders",
        "SELECT id FROM other.orders",
        "SELECT id FROM main.orders WHERE id = :id",
        "SELECT id FROM orders",
    ],
)
def test_structural_guard_rejects_unsafe_or_unknown_sql(database, sql):
    _, metadata, _ = database
    with pytest.raises(GenerationError):
        validate_sql(sql, "postgres", metadata, "main")


def test_count_star_allowed_and_fences_removed(database):
    _, metadata, _ = database
    sql = "SELECT COUNT(*) AS total FROM main.orders"
    assert validate_sql(f"```sql\n{sql}\n```", "postgres", metadata, "main") == sql


def test_postgres_quoted_names_preserve_database_case():
    metadata = MetaData()
    Table("Orders", metadata, Column("ID", Integer), schema="main")
    sql = 'SELECT "ID" FROM main."Orders"'
    assert validate_sql(sql, "postgres", metadata, "main") == sql
    with pytest.raises(GenerationError):
        validate_sql("SELECT ID FROM main.Orders", "postgres", metadata, "main")


def test_sqlserver_identifier_case_is_normalized():
    metadata = MetaData()
    Table("Orders", metadata, Column("ID", Integer), schema="dbo")
    sql = "SELECT ID FROM DBO.ORDERS"
    assert validate_sql(sql, "tsql", metadata, "dbo") == sql


def test_truncated_model_response_rejected(database):
    engine, _, _ = database
    model = Model(responses=["SELECT id FROM main.orders"], reason="length")
    with pytest.raises(GenerationError, match="finish generating"):
        Generator(settings(), engine=engine, model=model).generate("Order IDs")


def test_prompt_is_external_and_question_is_a_separate_message(database):
    engine, _, _ = database
    messages = []

    class InspectModel(Model):
        def _generate(self, received, *args, **kwargs):
            messages.extend(received)
            return super()._generate(received, *args, **kwargs)

    question = "Return order IDs above 10."
    model = InspectModel(responses=["SELECT id FROM main.orders WHERE id > 10"])
    Generator(settings(), engine=engine, model=model).generate(question)
    assert module.SQL_PROMPT.input_variables == ["dialect", "input", "table_info", "top_k"]
    assert messages[0].type == "system" and "CREATE TABLE" in messages[0].content
    assert messages[1].type == "human" and question in messages[1].content
    assert question not in messages[0].content


def test_unanswerable_request_has_actionable_error(database):
    engine, _, _ = database
    model = Model(responses=["CANNOT_GENERATE"])
    with pytest.raises(GenerationError, match="clearer business rules"):
        Generator(settings(), engine=engine, model=model).generate(
            "Unknown business KPI")


def test_700_columns_are_narrowed_before_prompt(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'wide.db'}")
    metadata = MetaData()
    Table(
        "wide",
        metadata,
        Column("id", Integer, primary_key=True),
        *(Column(f"c{i}", Integer) for i in range(699 + 1)),
    )
    metadata.create_all(engine)
    model = Model(responses=["SELECT id, c699 FROM main.wide"])
    try:
        result = Generator(settings(), engine=engine, model=model).generate(
            "Get id and c699")
        assert result["execution_performed"] is False
        assert len(model.selections[0]) == 701
        assert "c699" in model.prompts[0] and "c698" not in model.prompts[0]
    finally:
        engine.dispose()


def test_3000_tables_are_selected_in_bounded_batches(database, monkeypatch):
    engine, _, _ = database
    real_inspector = module.inspect(engine)

    class LargeCatalog:
        def get_table_names(self, **kwargs):
            return [f"irrelevant_{i:04}" for i in range(2999)] + ["target"]

        def __getattr__(self, name):
            return getattr(real_inspector, name)

    # The catalog enumeration is synthetic; selected-table reflection remains real.
    Table("target", MetaData(), Column("id", Integer, primary_key=True)).create(engine)
    monkeypatch.setattr(module, "inspect", lambda unused: LargeCatalog())
    model = Model(responses=["SELECT id FROM main.target"])
    result = Generator(settings(), engine=engine, model=model).generate("Target IDs")
    assert result["tables"] == ["main.target"]
    assert len(model.selections) == 12
    assert all(len(batch) <= 250 for batch in model.selections)
    assert "irrelevant_" not in model.prompts[0]


def test_generator_accepts_only_the_business_question():
    import inspect
    assert list(inspect.signature(Generator.generate).parameters) == ["self", "question"]


def test_selection_rejects_invented_metadata(database, monkeypatch):
    engine, _, _ = database
    model = Model(responses=["unused"])
    monkeypatch.setattr(
        Model,
        "with_structured_output",
        lambda *a, **k: RunnableLambda(lambda _: Selection(names=["invented"])),
    )
    with pytest.raises(GenerationError, match="unavailable metadata"):
        Generator(settings(), engine=engine, model=model)._select(
            "Question", ["a", "b"], "tables", 1, time.monotonic() + 10
        )


def test_settings_validate_and_redact(monkeypatch):
    monkeypatch.setattr("MCP.settings.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:secret@localhost/example")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-api-key")
    monkeypatch.delenv("DATABASE_SCHEMA", raising=False)
    # Obsolete fixed-scope variables must have no effect on the new request contract.
    monkeypatch.setenv("DATABASE_SCHEMA", "obsolete")
    monkeypatch.setenv("DATABASE_TABLES", "obsolete")
    loaded = Settings.load()
    assert not hasattr(loaded, "schema") and not hasattr(loaded, "tables")
    assert "secret" not in repr(loaded) and "private-api-key" not in repr(loaded)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:secret@remote/example")
    with pytest.raises(ValueError, match="verified TLS") as error:
        Settings.load()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("option", ["odbc_connect", "ODBC_CONNECT"])
def test_settings_reject_embedded_odbc_override(monkeypatch, option):
    monkeypatch.setattr("MCP.settings.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setenv(
        "DATABASE_URL",
        "mssql+pyodbc://reader:test-only@db.example.com/erp?"
        f"driver=ODBC+Driver+18+for+SQL+Server&{option}=Encrypt%3Dno",
    )
    with pytest.raises(ValueError, match="verified TLS"):
        Settings.load()


@pytest.mark.parametrize("option", ["host", "hostaddr", "HOST", "HOSTADDR"])
def test_settings_reject_postgres_host_override(monkeypatch, option):
    monkeypatch.setattr("MCP.settings.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-only")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+psycopg://reader:offline@localhost/erp?{option}=remote.example.com",
    )
    with pytest.raises(ValueError, match="verified TLS"):
        Settings.load()


def test_mcp_error_does_not_leak_provider_or_database_secrets(monkeypatch):
    import asyncio

    from MCP import server as server_module

    class BrokenGenerator:
        def __init__(self, settings):
            self.engine = None

        def generate(self, *args):
            raise RuntimeError("postgres://user:password@host db rows and secret-api-key")

    monkeypatch.setattr(server_module, "Generator", BrokenGenerator)
    server = server_module.create_server(settings())
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as error:
        asyncio.run(server.call_tool("generate_sql", {"question": "Orders"}))
    rendered = str(error.value)
    assert "Generation failed" in rendered
    assert "password" not in rendered and "secret-api-key" not in rendered


def test_mcp_only_accepts_question_and_uses_automatic_scope(monkeypatch):
    import asyncio

    from MCP import server as server_module

    class ScopedGenerator:
        def __init__(self, settings):
            self.engine = None

        def generate(self, question):
            assert question == "IDs"
            return {
                "sql": "SELECT id FROM archive.orders",
                "schema": "archive",
                "execution_performed": False,
            }

    monkeypatch.setattr(server_module, "Generator", ScopedGenerator)
    server = server_module.create_server(settings())
    tools = asyncio.run(server.list_tools())
    assert set(tools[0].input_schema["properties"]) == {"question"}
    result = asyncio.run(
        server.call_tool(
            "generate_sql",
            {
                "question": "IDs",
            },
        )
    )
    assert result.structured_content["schema"] == "archive"
    assert result.structured_content["execution_performed"] is False


@pytest.mark.parametrize(
    "sql,dialect",
    [
        (
            "WITH changed AS (DELETE FROM main.orders RETURNING id) SELECT id FROM changed",
            "postgres",
        ),
        ("SELECT id FROM main.orders FOR UPDATE", "postgres"),
        ("SELECT id INTO main.copy FROM main.orders", "tsql"),
        ("SELECT COUNT(*) FROM main.missing", "postgres"),
        ("SELECT id FROM main.orders UNION ALL SELECT COUNT(*) FROM main.missing", "postgres"),
    ],
)
def test_guard_blocks_nested_writes_locks_and_unknown_count_sources(database, sql, dialect):
    with pytest.raises(GenerationError):
        validate_sql(sql, dialect, database[1], "main")


def test_narrowing_preserves_foreign_key_to_non_primary_unique_column(tmp_path):
    from sqlalchemy import ForeignKey

    engine = create_engine(f"sqlite:///{tmp_path / 'foreign.db'}")
    metadata = MetaData()
    Table(
        "parent",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("business_key", Integer, unique=True),
        *(Column(f"extra{i}", Integer) for i in range(100)),
    )
    Table(
        "child",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_key", Integer, ForeignKey("parent.business_key")),
    )
    metadata.create_all(engine)
    sql = "SELECT p.id FROM main.parent p JOIN main.child c ON c.parent_key = p.business_key"
    model = Model(responses=[sql])  # Selector intentionally returns only the parent's id.
    try:
        result = Generator(settings(), engine=engine, model=model).generate(
            "Parent IDs with children")
        assert result["sql"] == sql
        assert "business_key" in model.prompts[0]
        assert "FOREIGN KEY(parent_key) REFERENCES parent (business_key)" in model.prompts[0]
        assert "extra99" not in model.prompts[0]
    finally:
        engine.dispose()


def test_sqlserver_sets_dbapi_statement_timeout(monkeypatch):
    from types import SimpleNamespace

    engine = SimpleNamespace(dialect=SimpleNamespace(name="mssql"))
    listeners = []
    monkeypatch.setattr(module, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(
        module.event,
        "listens_for",
        lambda target, name: lambda callback: listeners.append((target, name, callback)),
    )
    Generator(Settings("mssql+pyodbc://offline", "offline"), model=Model(responses=["unused"]))
    assert len(listeners) == 1 and listeners[0][:2] == (engine, "connect")
    connection = SimpleNamespace(timeout=0)
    listeners[0][2](connection, None)
    assert connection.timeout == 10


def test_real_deepseek_adapter_json_selection_and_sql_generation(tmp_path):
    import httpx
    from langchain_deepseek import ChatDeepSeek

    engine = create_engine(f"sqlite:///{tmp_path / 'adapter.db'}")
    metadata = MetaData()
    Table(
        "wide",
        metadata,
        Column("id", Integer, primary_key=True),
        *(Column(f"c{i}", Integer) for i in range(100)),
    )
    metadata.create_all(engine)
    requests = []
    sql = "SELECT id, c99 FROM main.wide"

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        structured = body.get("response_format") == {"type": "json_object"}
        content = json.dumps({"names": ["id", "c99"]}) if structured else sql
        return httpx.Response(
            200,
            json={
                "id": "offline-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ChatDeepSeek(
            model="deepseek-flash",
            api_key="offline-key",
            base_url="https://api.deepseek.com",
            http_client=client,
            temperature=0,
            max_retries=0,
            max_tokens=8192,
            extra_body={"thinking": {"type": "disabled"}},
        )
        try:
            result = Generator(settings(), engine=engine, model=model).generate(
                "IDs and c99")
        finally:
            engine.dispose()
    assert result["sql"] == sql and not result["execution_performed"]
    assert len(requests) == 2
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert all(body["thinking"] == {"type": "disabled"} for body in requests)
    assert "c98" not in requests[1]["messages"][0]["content"]


def scoped_database(tmp_path, scopes):
    """SQLite fixtures reproduce an empty default schema without external services."""
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        for scope in scopes:
            connection.exec_driver_sql(f"ATTACH DATABASE '{tmp_path / (scope + '.db')}' AS {scope}")
            connection.exec_driver_sql(f"CREATE TABLE {scope}.orders(id integer PRIMARY KEY, amount numeric)")
    return engine


def test_question_only_discovers_data_outside_empty_default_schema(tmp_path):
    engine = scoped_database(tmp_path, ["erp"])
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, p, ctx, many: statements.append(sql))
    try:
        generator = Generator(settings(), engine=engine, model=Model(responses=["SELECT SUM(amount) AS total FROM erp.orders"]))
        result = generator.generate("What is the total order value?")
        assert result["schema"] == "erp" and result["execution_performed"] is False
        assert not any("from erp.orders" in sql.lower() for sql in statements)
    finally:
        engine.dispose()


@pytest.mark.parametrize("choice", ["erp", "archive"])
def test_question_only_selects_schema_per_request_without_caching(tmp_path, choice):
    engine = scoped_database(tmp_path, ["erp", "archive"])

    class ScopeModel(Model):
        def with_structured_output(self, schema, **kwargs):
            def choose(messages):
                payload = json.loads(messages[-1][1])
                assert set(payload["catalog"]) == {"erp", "archive"}
                assert "main" not in payload["catalog"]
                return ScopeDecision(outcome="selected", names=["archive" if "archived" in payload["request"] else "erp"])
            return RunnableLambda(choose)

    other = "archive" if choice == "erp" else "erp"
    model = ScopeModel(responses=[f"SELECT id FROM {choice}.orders", f"SELECT id FROM {other}.orders"])
    generator = Generator(settings(), engine=engine, model=model)
    try:
        for scope in [choice, other]:
            question = "Show archived orders" if scope == "archive" else "Show current orders"
            assert generator.generate(question)["schema"] == scope
    finally:
        engine.dispose()


@pytest.mark.parametrize("choices", [[], ["missing"], ["erp", "erp"]])
def test_ambiguous_or_invented_schema_selection_never_generates_sql(tmp_path, choices):
    engine = scoped_database(tmp_path, ["erp", "archive"])

    class AmbiguousModel(Model):
        def with_structured_output(self, schema, **kwargs):
            return RunnableLambda(lambda _: ScopeDecision(outcome="selected", names=choices))

    model = AmbiguousModel(responses=["must never be generated"])
    try:
        with pytest.raises(GenerationError, match="invalid source selection"):
            Generator(settings(), engine=engine, model=model).generate("Show orders")
        assert model.prompts == []
    finally:
        engine.dispose()


def test_empty_database_reports_configuration_problem_without_requesting_identifiers():
    engine = create_engine("sqlite://")
    try:
        with pytest.raises(GenerationError, match="administrator") as error:
            Generator(settings(), engine=engine, model=Model(responses=["unused"])).generate("Show orders")
        assert "table" not in str(error.value).lower() and "schema" not in str(error.value).lower()
    finally:
        engine.dispose()


def test_empty_default_scope_does_not_authorize_invented_objects(tmp_path):
    engine = scoped_database(tmp_path, ["erp"])
    try:
        with pytest.raises(GenerationError, match="schema checks"):
            Generator(settings(), engine=engine, model=Model(responses=["SELECT id FROM main.orders"])).generate("Show orders")
    finally:
        engine.dispose()


def test_auto_scope_still_rejects_cross_schema_output(tmp_path):
    engine = scoped_database(tmp_path, ["erp"])
    try:
        with pytest.raises(GenerationError, match="schema checks"):
            Generator(settings(), engine=engine, model=Model(responses=["SELECT id FROM archive.orders"])).generate("Show orders")
    finally:
        engine.dispose()


def test_schema_discovery_budget_precedes_model_generation(database, monkeypatch):
    engine, _, _ = database
    real = module.inspect(engine)

    class OversizedCatalog:
        def get_table_names(self, **kwargs):
            return [f"t{i}" for i in range(10001)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(module, "inspect", lambda _: OversizedCatalog())
    model = Model(responses=["unused"])
    with pytest.raises(GenerationError, match="administrator to narrow access"):
        Generator(settings(), engine=engine, model=model).generate("Show orders")
    assert not model.prompts


def test_mcp_question_only_handles_empty_default_schema(tmp_path, monkeypatch):
    import asyncio
    from MCP import server as server_module

    engine = scoped_database(tmp_path, ["erp"])
    generator = Generator(settings(), engine=engine, model=Model(responses=["SELECT SUM(amount) AS total FROM erp.orders"]))
    monkeypatch.setattr(server_module, "Generator", lambda _: generator)
    server = server_module.create_server(settings())
    try:
        result = asyncio.run(server.call_tool("generate_sql", {"question": "What is the total order value?"}))
        assert result.structured_content["schema"] == "erp"
        assert result.structured_content["execution_performed"] is False
    finally:
        engine.dispose()


def test_execution_events_cover_stages_without_logging_business_content(database, caplog):
    import logging
    engine, _, _ = database
    question = "private-business-question-marker"
    sql = "SELECT id FROM main.orders"
    with caplog.at_level(logging.INFO, logger=module.LOGGER.name):
        Generator(settings(), engine=engine, model=Model(responses=[sql])).generate(question)
    entries = [json.loads(record.message) for record in caplog.records if record.name == module.LOGGER.name]
    events = {entry["event"] for entry in entries}
    assert {"generation.started", "metadata.discovery.started", "tables.selected", "columns.selected",
            "context.build.completed", "sql.generation.started", "sql.validation.completed", "generation.completed"} <= events
    rendered = json.dumps(entries)
    leaked = any(value in rendered for value in [question, sql, settings().api_key, module.PROMPTS["sql"]])
    assert not leaked, "Protected content appeared in diagnostic events"
    assert entries[-1]["execution_performed"] is False
    assert entries[-1]["elapsed_ms"] >= 0


@pytest.mark.parametrize("private_error", ["private-provider-error-with-password-and-url-marker", "TOKEN"])
def test_dependency_failure_logs_stage_and_type_without_raw_error(database, caplog, private_error):
    import logging
    engine, _, _ = database

    class BrokenModel(Model):
        def _generate(self, *args, **kwargs):
            raise RuntimeError(private_error)

    with caplog.at_level(logging.INFO, logger=module.LOGGER.name):
        with pytest.raises(RuntimeError):
            Generator(settings(), engine=engine, model=BrokenModel(responses=["unused"])).generate("Order IDs")
    entries = [json.loads(record.message) for record in caplog.records if record.name == module.LOGGER.name]
    failure = entries[-1]
    assert failure["event"] == "generation.failed"
    assert failure["stage"] == "sql_generation" and failure["error_type"] == "RuntimeError"
    assert failure["sqlstate"] is None
    leaked = private_error in json.dumps(entries)
    assert not leaked, "Raw dependency error appeared in diagnostic events"


@pytest.mark.parametrize("outcome,reason", [("ambiguous", "business_scope_ambiguous"), ("no_match", "metadata_no_match")])
def test_business_source_clarification_is_structured_not_a_tool_error(tmp_path, monkeypatch, outcome, reason):
    import asyncio
    from MCP import server as server_module
    engine = scoped_database(tmp_path, ["erp", "archive"])

    class ClarifyingModel(Model):
        def with_structured_output(self, schema, **kwargs):
            assert schema is ScopeDecision
            return RunnableLambda(lambda _: ScopeDecision(outcome=outcome, names=[]))

    model = ClarifyingModel(responses=["must not generate SQL"])
    generator = Generator(settings(), engine=engine, model=model)
    monkeypatch.setattr(server_module, "Generator", lambda _: generator)
    try:
        response = asyncio.run(server_module.create_server(settings()).call_tool("generate_sql", {"question": "A 360-degree overview of each company"}))
        data = response.structured_content
        assert data["status"] == "needs_clarification" and data["reason"] == reason
        assert data["execution_performed"] is False and "sql" not in data
        assert "archiv" not in data["clarification"].lower()
        assert not model.prompts
    finally:
        engine.dispose()


def test_missing_business_rules_return_structured_clarification(database, monkeypatch):
    import asyncio
    from MCP import server as server_module
    engine, _, _ = database
    generator = Generator(settings(), engine=engine, model=Model(responses=["CANNOT_GENERATE"]))
    monkeypatch.setattr(server_module, "Generator", lambda _: generator)
    response = asyncio.run(server_module.create_server(settings()).call_tool("generate_sql", {"question": "Give a complete business overview"}))
    assert response.structured_content["status"] == "needs_clarification"
    assert response.structured_content["reason"] == "business_rules_incomplete"
    assert "sql" not in response.structured_content


def test_requests_do_not_send_previous_questions_or_selections_to_provider(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from langchain_deepseek import ChatDeepSeek
    from MCP import server as server_module
    engine = scoped_database(tmp_path, ["erp", "archive"])
    messages = []
    inspections = []
    real_inspect = module.inspect
    monkeypatch.setattr(module, "inspect", lambda e: (inspections.append(e), real_inspect(e))[1])

    def respond(request):
        body = json.loads(request.content)
        messages.append(body["messages"])
        if body.get("response_format") == {"type": "json_object"}:
            payload = json.loads(body["messages"][-1]["content"])
            outcome = "ambiguous" if "360" in payload["request"] else "selected"
            content = json.dumps({"outcome": outcome, "names": [] if outcome == "ambiguous" else ["erp"]})
        else:
            content = "SELECT SUM(amount) AS total FROM erp.orders"
        return httpx.Response(200, json={"id": "isolated", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ChatDeepSeek(model="deepseek-flash", api_key="isolated-only", http_client=client,
                             max_retries=0, temperature=0, extra_body={"thinking": {"type": "disabled"}})
        generator = Generator(settings(), engine=engine, model=model)
        monkeypatch.setattr(server_module, "Generator", lambda _: generator)
        server = server_module.create_server(settings())
        try:
            questions = ["Give a 360-degree company overview", "Total current order value", "Give a 360-degree company overview"]
            results = [asyncio.run(server.call_tool("generate_sql", {"question": q})).structured_content for q in questions]
            assert [result["status"] for result in results] == ["needs_clarification", "generated", "needs_clarification"]
            assert results[0] == results[2]
            assert len(inspections) == 3
            assert len(messages) == 4 and messages[0] == messages[3]
            assert all(len(m) == 2 for m in messages)
            assert questions[0] not in json.dumps(messages[1:3])
        finally:
            engine.dispose()


def test_settings_load_only_the_project_dotenv(monkeypatch):
    calls = []
    monkeypatch.setattr("MCP.settings.load_dotenv", lambda path, **kwargs: calls.append(path))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://reader:isolated@localhost/isolated")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "isolated-key")
    Settings.load()
    assert len(calls) == 1 and calls[0].name == ".env"


def test_selection_repairs_one_invalid_batch_without_accepting_invented_names(database, monkeypatch):
    engine, _, _ = database
    calls = []
    def choose(messages):
        calls.append(messages[0][1])
        return Selection(names=["outside_batch"] if len(calls) == 1 else ["a"])
    model = Model(responses=["unused"])
    monkeypatch.setattr(Model, "with_structured_output", lambda *a, **k: RunnableLambda(choose))
    result = Generator(settings(), engine=engine, model=model)._select(
        "Business question", ["a", "b"], "table names", 1, time.monotonic() + 10
    )
    assert result == ["a"]
    assert len(calls) == 2
    assert "previous selection violated" in calls[1]
    assert "previous selection violated" not in calls[0]
