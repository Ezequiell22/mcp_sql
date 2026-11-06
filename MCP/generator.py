"""LangChain selects metadata and generates SQL. Generated SQL is never executed."""

import json
import logging
import multiprocessing
import os
import ssl
from contextvars import ContextVar
from datetime import datetime, timezone
import re
import threading
import time
import tomllib
from pathlib import Path
from typing import Literal

import sqlglot
from langchain_classic.chains import create_sql_query_chain
from langchain_community.utilities import SQLDatabase
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_deepseek import ChatDeepSeek
from pydantic import BaseModel, Field
from sqlalchemy import MetaData, Table, create_engine, event, inspect
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url
from sqlalchemy.types import NullType
from sqlglot import exp
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema

from MCP.settings import DIALECTS, Settings

BATCH_SIZE = 250
MAX_COLUMNS = 80
MAX_CONTEXT_BYTES = 64_000
REQUEST_TIMEOUT = 120

# Explicit built-in families only. Unknown/qualified routine calls fail closed.
READ_FUNCTIONS = frozenset("""
ABS ACOS ASIN ATAN ATAN2 AVG CEIL CEILING FLOOR ROUND TRUNC POW POWER SQRT SIGN MOD
EXP LN LOG LOG10 GREATEST LEAST SUM MIN MAX COUNT COALESCE NULLIF IF IFNULL ISNULL
CASE CAST TRY_CAST CONVERT EXTRACT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
CURRENT_DATETIME DATE DATE_ADD DATE_SUB DATE_DIFF DATE_TRUNC DATE_PART
TIMESTAMP TIMESTAMP_ADD TIMESTAMP_SUB TIMESTAMP_DIFF TIMESTAMP_TRUNC
TIME TIME_ADD TIME_SUB TIME_DIFF TIME_TRUNC INTERVAL STR_TO_DATE TIME_TO_STR
TS_OR_DS_TO_TIMESTAMP TS_OR_DS_TO_DATE TS_OR_DS_TO_TIME
YEAR QUARTER MONTH DAY DAY_OF_MONTH DAY_OF_WEEK DAY_OF_YEAR WEEK WEEK_OF_YEAR
HOUR MINUTE SECOND LAST_DAY MONTHS_BETWEEN ADD_MONTHS DATEDIFF
LOWER UPPER LENGTH CHAR_LENGTH CHARACTER_LENGTH OCTET_LENGTH TRIM LTRIM RTRIM
SUBSTRING LEFT RIGHT REPLACE CONCAT CONCAT_WS SPLIT_PART STR_POSITION POSITION
STARTSWITH ENDSWITH LIKE ILIKE REGEXP_LIKE REGEXP_REPLACE REGEXP_EXTRACT
REPEAT REVERSE LPAD RPAD ASCII CHR CHAR UNICODE SPACE
ROW_NUMBER RANK DENSE_RANK NTILE LAG LEAD FIRST_VALUE LAST_VALUE NTH_VALUE
PERCENT_RANK CUME_DIST PERCENTILE_CONT PERCENTILE_DISC STDDEV STDDEV_POP
STDDEV_SAMP VARIANCE VAR_POP VAR_SAMP ARRAY_AGG STRING_AGG GROUP_CONCAT
BOOL_AND BOOL_OR EVERY ANY_VALUE ARRAY_SIZE JSON_EXTRACT JSON_EXTRACT_SCALAR
JSON_VALUE JSON_QUERY JSON_ARRAY_LENGTH FILTER WITHIN_GROUP EXISTS
""".split())
# SQLGlot leaves some dialect built-ins anonymous. Never permit arbitrary names.
ANONYMOUS_READ_FUNCTIONS = {
    "postgres": frozenset({"date_part", "date_trunc", "age", "to_char", "to_date", "to_timestamp"}),
    "tsql": frozenset({"datediff_big", "eomonth", "datefromparts", "datetimefromparts", "isnumeric", "len"}),
    "mysql": frozenset({"date_format", "str_to_date", "timestampdiff", "timestampadd"}),
    "sqlite": frozenset({"strftime", "julianday", "unixepoch", "datetime"}),
}

REQUEST_ID = ContextVar("request_id", default=None)
STAGE = ContextVar("generation_stage", default="idle")
LOGGER = logging.getLogger("atlas_sql_mcp")


def log_event(event, *, level=logging.INFO, stage=None, **fields):
    """Log only application-owned fields; never pass requests, SQL or exceptions here."""
    if stage is not None:
        STAGE.set(stage)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": logging.getLevelName(level),
        "event": event,
        "request_id": REQUEST_ID.get(),
        "stage": STAGE.get(),
        **fields,
    }
    LOGGER.log(level, json.dumps(record, ensure_ascii=True))


class GenerationError(Exception):
    """Safe, application-authored message suitable for MCP clients."""


class Selection(BaseModel):
    names: list[str] = Field(default_factory=list)


class ScopeDecision(BaseModel):
    outcome: Literal["selected", "ambiguous", "no_match"]
    names: list[str] = Field(default_factory=list, max_length=80)


class ClarificationNeeded(GenerationError):
    """A normal, stateless business clarification, not an infrastructure failure."""

    def __init__(self, message, reason="business_rules_incomplete"):
        super().__init__(message)
        self.reason = reason


PROMPTS = tomllib.loads(Path(__file__).with_name("prompts.toml").read_text())
SQL_PROMPT = ChatPromptTemplate.from_messages([("system", PROMPTS["sql"]), ("human", "{input}")])


def validate_sql(sql, dialect, metadata, schema, *, default_schema=None):
    """Small structural guard, not a database compiler or business-correctness proof."""
    sql = sql.strip()
    fenced = re.fullmatch(r"```(?:sql)?\s*\n(.*?)\n```", sql, re.S | re.I)
    if fenced:
        sql = fenced.group(1).strip()
    if sql == "CANNOT_GENERATE":
        raise ClarificationNeeded(
            "The request needs clearer business rules, such as the ranking measure, filters or period."
        )
    if not sql or len(sql.encode()) > 64_000:
        raise GenerationError(
            "The model did not return a complete SQL query within the output budget."
        )
    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
        if any(comment.lstrip().startswith(("!", "+", "M!", "m!"))
               for token in tokens for comment in (token.comments or [])):
            # MySQL executable comments can hide semantics absent from the AST.
            # Token comments distinguish them from ordinary quoted string data.
            raise ValueError
        statements = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
            raise ValueError
        tree = statements[0]
        normalize_identifiers(tree, dialect=dialect)
        if any(
            isinstance(n, (exp.DDL, exp.DML, exp.Command, exp.Into, exp.Lock)) for n in tree.walk()
        ):
            raise ValueError
        if any(isinstance(n, (exp.Parameter, exp.Placeholder)) for n in tree.walk()):
            raise ValueError
        # SQLGlot resolves CTEs/aliases and rejects unknown tables/columns.
        normalize = str.casefold if dialect == "tsql" else lambda name: name
        columns = {}
        for table in metadata.tables.values():
            table_schema = table.schema or (schema if isinstance(schema, str) else None)
            if not table_schema:
                raise ValueError
            columns.setdefault(normalize(table_schema), {})[normalize(table.name)] = {
                normalize(column.name): str(column.type) for column in table.columns
            }
        for node in tree.walk():
            if isinstance(node, (exp.Hint, exp.WithTableHint, exp.QueryOption, exp.NextValueFor)):
                raise ValueError
            if isinstance(node, exp.Func):
                if isinstance(node.parent, exp.Dot):
                    raise ValueError
                if isinstance(node, exp.Anonymous):
                    if node.name.casefold() not in ANONYMOUS_READ_FUNCTIONS.get(dialect, ()):
                        raise ValueError
                elif node.sql_name() not in READ_FUNCTIONS:
                    raise ValueError
        for table in tree.find_all(exp.Table):
            if table.catalog or (table.db and table.db not in columns):
                raise ValueError
            if not isinstance(table.this, exp.Identifier):
                raise ValueError
        qualified_default = False
        for scope in traverse_scope(tree):
            for source in scope.sources.values():
                if isinstance(source, exp.Table):
                    if not source.db and default_schema and normalize(default_schema) in columns:
                        # The latest user contract authorizes filling an omitted
                        # physical schema from this connection's verified default.
                        source.set("db", exp.to_identifier(default_schema, quoted=True))
                        qualified_default = True
                    source_schema = normalize(source.db)
                    if source_schema not in columns or source.name not in columns[source_schema]:
                        raise ValueError
        qualify(
            tree.copy(),
            dialect=dialect,
            schema=MappingSchema(columns, dialect=dialect, normalize=False),
            db=normalize(schema) if isinstance(schema, str) else None,
            validate_qualify_columns=True,
            allow_partial_qualification=False,
        )
        # Projection wildcards are forbidden; COUNT(*) is legitimate.
        if any(
            isinstance(n, exp.Star) and not isinstance(n.parent, exp.Count) for n in tree.walk()
        ):
            raise ValueError
        if qualified_default:
            sql = tree.sql(dialect=dialect, unsupported_level=ErrorLevel.RAISE)
            if len(sql.encode()) > 64_000:
                raise ValueError
    except Exception:
        raise GenerationError(
            "Generated SQL failed SELECT-only or schema checks; refine the request."
        ) from None
    return sql


def complete_message(message):
    if message.response_metadata.get("finish_reason") != "stop":
        raise GenerationError("The model did not finish generating SQL; narrow the request.")
    return message


class ReflectedDatabase(SQLDatabase):
    """Use request-verified objects, including views unsupported by LC enumeration."""

    def __init__(self, engine, metadata, schema, names):
        self.names = tuple(names)
        # LC 0.4.2 unconditionally asks for materialized views with view_support;
        # several dialects raise NotImplementedError. Discovery and reflection
        # already verified these names; its public context renderer is reused.
        super().__init__(engine, schema=schema, metadata=metadata,
                         sample_rows_in_table_info=0, lazy_table_reflection=True)

    def get_usable_table_names(self):
        return self.names


class RequestDatabase(SQLDatabase):
    """LangChain context assembled from real reflection, retained only for this call."""

    def __init__(self, engine, context):
        self._engine = engine
        self.context = context

    def get_table_info(self, table_names=None, **kwargs):
        if table_names is not None:
            raise GenerationError("Request context cannot be respecified.")
        return self.context


def generation_worker(connection, settings, question, request_id):
    # Child output must never enter the MCP protocol; only safe structured logs
    # go to stderr. Settings travel through multiprocessing, never command args.
    from MCP.server import configure_logging
    configure_logging()
    REQUEST_ID.set(request_id)
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
    generator = None
    try:
        generator = Generator(settings, isolate=False)
        connection.send(("generated", generator.generate(question)))
    except ClarificationNeeded as error:
        connection.send(("clarification", (str(error), error.reason)))
    except GenerationError as error:
        connection.send(("rejected", str(error)))
    except Exception:
        connection.send(("rejected", "Generation failed; check database access and DeepSeek configuration."))
    finally:
        if generator is not None:
            generator.engine.dispose()
        connection.close()


class Generator:
    def __init__(self, settings: Settings, *, engine=None, model=None, isolate=True):
        self.settings = settings
        self.isolate = isolate and engine is None and model is None
        backend = settings.database_url.split(":", 1)[0]
        # Injected engines are isolated test fixtures; production drivers are explicit.
        if engine is None and backend not in {spec["driver"] for spec in DIALECTS.values()}:
            raise GenerationError("Unsupported database driver; consult the compatibility matrix.")
        postgres = backend == "postgresql+psycopg"
        if postgres:
            connect_args = {
                "connect_timeout": 10,
                "options": "-c default_transaction_read_only=on -c statement_timeout=10000",
            }
        elif backend == "mssql+pyodbc":
            connect_args = {"timeout": 10}
        elif backend == "mysql+pymysql":
            connect_args = {
                "connect_timeout": 10, "read_timeout": 10, "write_timeout": 10,
                # SQLAlchemy converts ssl_ca into a dictionary, while PyMySQL's
                # separate verify flags can replace that dictionary. One explicit
                # verified SSLContext preserves both CA and hostname checks.
                "ssl": ssl.create_default_context(cafile=make_url(settings.database_url).query.get("ssl_ca")),
                "local_infile": False,
                "init_command": "SET SESSION TRANSACTION READ ONLY",
            }
        else:
            connect_args = {}
        self.engine = (
            engine
            if engine is not None
            else create_engine(
                settings.database_url,
                poolclass=NullPool,
                hide_parameters=True,
                connect_args=connect_args,
            )
        )
        if engine is None and self.engine.dialect.name == "mssql":

            @event.listens_for(self.engine, "connect")
            def statement_timeout(connection, record):
                connection.timeout = 10

        self.model = (
            model
            if model is not None
            else ChatDeepSeek(
                model=settings.model,
                api_key=settings.api_key,
                temperature=0,
                base_url="https://api.deepseek.com",
                timeout=30,
                max_retries=0,
                max_tokens=8192,
                extra_body={"thinking": {"type": "disabled"}},
            )
        )
        self.lock = threading.Lock()

    def _select(self, question, candidates, purpose, limit, deadline):
        if time.monotonic() >= deadline:
            raise GenerationError("This request took too long; try a more focused business question.")
        selection_stage = "table_selection" if "table names" in purpose else "column_selection"
        started = time.monotonic()
        log_event("selection.started", stage=selection_stage, candidate_count=len(candidates), limit=limit)
        if len(candidates) <= limit and ("table names" not in purpose or len(candidates) <= 1):
            log_event("selection.completed", selected_count=len(candidates), provider_call=False, elapsed_ms=0)
            return list(candidates)
        messages = [
            ("system", PROMPTS["selection"].format(limit=limit, purpose=purpose)),
            ("human", json.dumps({"request": question, "candidates": candidates}, ensure_ascii=False)),
        ]
        chooser = self.model.with_structured_output(Selection, method="json_mode")
        for attempt in range(2):
            if time.monotonic() >= deadline:
                raise GenerationError("This request took too long; try a more focused business question.")
            if len(messages[-1][1].encode()) > MAX_CONTEXT_BYTES:
                raise GenerationError("Metadata selection exceeds the context budget; ask the administrator to narrow access.")
            result = chooser.invoke(messages)
            names = list(dict.fromkeys(result.names))
            if len(names) <= limit and set(names) <= set(candidates):
                break
            log_event("selection.rejected", candidate_count=len(candidates),
                      selected_count=len(names), unavailable_count=len(set(names) - set(candidates)),
                      attempt=attempt + 1)
            if attempt or time.monotonic() >= deadline:
                raise GenerationError("The model selected unavailable metadata; refine the request.")
            messages[0] = ("system", messages[0][1] + "\n" + PROMPTS["selection_repair"])
            messages[1] = ("human", json.dumps({"request": question, "candidates": candidates,
                "rejected_names": names, "unavailable_names": sorted(set(names) - set(candidates)),
                "maximum_count": limit}, ensure_ascii=False))
        log_event("selection.completed", selected_count=len(names), provider_call=True,
                  elapsed_ms=round((time.monotonic() - started) * 1000))
        return names

    def generate(self, question: str):
        started = time.monotonic()
        stage_token = STAGE.set("validation")
        acquired = False
        log_event("generation.started")
        try:
            if not question.strip() or len(question.encode()) > 8000:
                raise GenerationError("The request must contain 1–8000 UTF-8 bytes.")
            acquired = self.lock.acquire(blocking=False)
            if not acquired:
                raise GenerationError("The server is busy generating SQL; retry shortly.")
            if self.isolate:
                result = self._isolated_generate(question, started)
            else:
                result = self._generate(question, started + REQUEST_TIMEOUT)
            log_event("generation.completed", stage="completed", elapsed_ms=round((time.monotonic() - started) * 1000),
                      table_count=len(result["tables"]), sql_bytes=len(result["sql"].encode()), execution_performed=False)
            return result
        except ClarificationNeeded as error:
            log_event("generation.clarification_required", reason=error.reason,
                      elapsed_ms=round((time.monotonic() - started) * 1000), execution_performed=False)
            raise
        except Exception as error:
            original = error.orig if isinstance(error, DBAPIError) else None
            sqlstate = getattr(original, "sqlstate", None)
            if sqlstate is None and getattr(original, "args", ()):
                candidate = original.args[0]
                if isinstance(candidate, str) and re.fullmatch(r"[A-Z0-9]{5}", candidate):
                    sqlstate = candidate
            if not isinstance(sqlstate, str) or not re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
                sqlstate = None
            http_status = getattr(error, "status_code", None)
            if not isinstance(http_status, int) or not 100 <= http_status <= 599:
                http_status = None
            log_event("generation.failed", level=logging.ERROR, error_type=type(error).__name__,
                      reason=str(error) if isinstance(error, GenerationError) else "Dependency failure; raw details suppressed.",
                      sqlstate=sqlstate, http_status=http_status,
                      failure_kind="request_rejected" if isinstance(error, GenerationError) else "dependency_failure",
                      elapsed_ms=round((time.monotonic() - started) * 1000), execution_performed=False)
            raise
        finally:
            if acquired:
                self.lock.release()
            STAGE.reset(stage_token)

    def _isolated_generate(self, question, started):
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        worker = context.Process(target=generation_worker, args=(
            send, self.settings, question, REQUEST_ID.get(),
        ), daemon=True)
        watchdog = None
        expired = threading.Event()

        def kill_overdue_worker():
            expired.set()
            try:
                worker.kill()
            except (OSError, ValueError):
                pass  # A worker that already exited needs no further cleanup.

        try:
            worker.start()
            send.close()
            remaining = max(0, REQUEST_TIMEOUT - (time.monotonic() - started))
            # poll() only bounds waiting for the first IPC bytes. A watchdog
            # also covers a worker stalled halfway through a large reply.
            watchdog = threading.Timer(remaining, kill_overdue_worker)
            watchdog.daemon = True
            watchdog.start()
            if not receive.poll(remaining):
                raise GenerationError("This request took too long; try a more focused business question.")
            try:
                outcome, payload = receive.recv()
            except (EOFError, OSError):
                if expired.is_set() or time.monotonic() - started >= REQUEST_TIMEOUT:
                    raise GenerationError("This request took too long; try a more focused business question.") from None
                raise GenerationError("Generation worker stopped; retry the complete request.") from None
            if time.monotonic() - started >= REQUEST_TIMEOUT:
                raise GenerationError("This request took too long; try a more focused business question.")
            if outcome == "clarification":
                raise ClarificationNeeded(*payload)
            if outcome != "generated":
                raise GenerationError(payload)
            return payload
        finally:
            if watchdog is not None:
                watchdog.cancel()
                watchdog.join()
            receive.close()
            send.close()
            if worker.pid is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=1)
                if worker.is_alive():
                    worker.kill()
                    worker.join(timeout=1)
                worker.close()

    def _generate(self, question, deadline):
        def check_budget():
            if time.monotonic() >= deadline:
                raise GenerationError("This request took too long; try a more focused business question.")

        log_event("metadata.discovery.started", stage="metadata_discovery")
        inspector = inspect(self.engine)
        available_schemas = inspector.get_schema_names()
        default_schema = inspector.default_schema_name
        if not default_schema:
            # Driver defaults are fallbacks only, never authorization to invent
            # an object absent from the actual selected metadata.
            default_schema = {"postgresql": "public", "mssql": "dbo", "sqlite": "main"}.get(
                self.engine.dialect.name, self.engine.url.database)


        def objects(name):
            check_budget()
            tables = set(inspector.get_table_names(schema=name))
            check_budget()
            views = set(inspector.get_view_names(schema=name))
            check_budget()
            try:
                views.update(inspector.get_materialized_view_names(schema=name))
            except NotImplementedError:
                pass
            return sorted(tables | views)

        catalog = {}
        count = 0
        for name in sorted(available_schemas):
            if name.casefold() in {"information_schema", "sys", "mysql", "performance_schema"} or name.startswith("pg_"):
                continue
            names = objects(name)
            count += len(names)
            if count > 10_000 or (names and len(catalog) >= 100):
                raise GenerationError("The connected data catalog is too large. Ask the administrator to narrow access.")
            if names:
                catalog[name] = names
        log_event("metadata.catalog.discovered", schema_count=len(catalog), table_count=count)
        if not catalog:
            raise GenerationError("No usable data is visible to this connection. Ask the administrator to check the database and reader permissions.")
        if len(catalog) == 1:
            scopes = list(catalog)
        else:
            context = json.dumps({"request": question, "catalog": catalog, "default_schema": default_schema}, ensure_ascii=False)
            if len(context.encode()) > MAX_CONTEXT_BYTES:
                raise GenerationError("The connected data catalog is too large. Ask the administrator to narrow access.")
            check_budget()
            scope_started = time.monotonic()
            log_event("schema.selection.started", stage="schema_selection", candidate_count=len(catalog), context_bytes=len(context.encode()))
            result = self.model.with_structured_output(ScopeDecision, method="json_mode").invoke(
                [("system", PROMPTS["schema_selection"]), ("human", context)])
            log_event("schema.selection.completed", outcome=result.outcome, selected_count=len(result.names),
                      elapsed_ms=round((time.monotonic() - scope_started) * 1000))
            if result.outcome != "selected":
                if result.names:
                    raise GenerationError("The model returned an inconsistent source selection.")
                if result.outcome == "no_match":
                    raise ClarificationNeeded("I could not match this request to the available business data. Explain what the business entity represents and which information or measures to include.", reason="metadata_no_match")
                raise ClarificationNeeded("Please define the business entity, information or measures, and period for this request. A broad overview needs a concrete business definition.", reason="business_scope_ambiguous")
            scopes = list(dict.fromkeys(result.names))
            if not scopes or len(scopes) > 8 or len(scopes) != len(result.names) or not set(scopes) <= set(catalog):
                raise GenerationError("The model returned an invalid source selection; retry the complete business question.")
        single_schema = scopes[0] if len(scopes) == 1 else None
        candidates = {}
        for scope in scopes:
            for name in catalog[scope]:
                key = name if single_schema else f"{scope}.{name}"
                if key in candidates:
                    raise GenerationError("Ambiguous catalog identifiers; ask the administrator to review object names.")
                candidates[key] = (scope, name)
        available = sorted(candidates)
        choices = []
        for start in range(0, len(available), BATCH_SIZE):
            batch = available[start:start + BATCH_SIZE]
            choices.extend(self._select(question, batch, "table names", len(batch), deadline))
        selected = choices if len(choices) <= BATCH_SIZE else self._select(
            question, choices, "table names", len(choices), deadline)
        if not selected:
            raise ClarificationNeeded("No data matching this question was identified. Describe the business records and measures you need.", "metadata_no_match")
        selected_objects = [candidates[key] for key in selected]
        log_event("tables.selected", stage="metadata_reflection", table_count=len(selected))
        metadata = MetaData()
        references = {}
        for scope, name in selected_objects:
            check_budget()
            references[(scope, name)] = inspector.get_foreign_keys(name, schema=scope)
        for scope, name in selected_objects:
            check_budget()
            columns = inspector.get_columns(name, schema=scope)
            names = [c["name"] for c in columns]
            if len(names) > 4096:
                raise GenerationError("Selected table exceeds the column discovery budget.")
            chosen = self._select(question, names, f"columns of {scope}.{name}", MAX_COLUMNS - 20, deadline)
            check_budget()
            keys = list(inspector.get_pk_constraint(name, schema=scope).get("constrained_columns") or [])
            for source, relationships in references.items():
                for fk in relationships:
                    target = (fk.get("referred_schema") or source[0], fk.get("referred_table"))
                    if source == (scope, name) and target in references:
                        keys.extend(fk["constrained_columns"])
                    if target == (scope, name):
                        keys.extend(fk["referred_columns"])
            chosen = list(dict.fromkeys(chosen + keys))
            if not chosen or len(chosen) > MAX_COLUMNS:
                raise GenerationError("Relevant columns exceed the context budget; narrow the request.")
            log_event("columns.selected", stage="metadata_reflection", column_count=len(chosen), available_count=len(names))
            check_budget()
            Table(name, metadata, schema=scope, autoload_with=self.engine, include_columns=chosen, resolve_fks=False)
        if any(isinstance(column.type, NullType) for table in metadata.tables.values() for column in table.columns):
            raise GenerationError("Selected metadata contains unsupported column types; ask the administrator to check driver reflection support.")

        # Omit relationships outside this request's reflected objects/columns.
        for table in metadata.tables.values():
            for constraint in list(table.foreign_key_constraints):
                try:
                    for fk in constraint.elements:
                        _ = fk.column
                except Exception:
                    table.constraints.remove(constraint)
                    for fk in constraint.elements:
                        table.foreign_keys.discard(fk)
                        fk.parent.foreign_keys.discard(fk)

        log_event("context.build.started", stage="metadata_context")
        contexts = []
        described_names = set()
        for scope in scopes:
            names = [name for selected_scope, name in selected_objects if selected_scope == scope]
            if not names:
                continue
            check_budget()
            scoped_db = ReflectedDatabase(self.engine, metadata, scope, names)
            for name in names:
                if name not in described_names:
                    # SQLDatabase includes every selected Table with this name,
                    # including namesakes in other selected schemas.
                    contexts.append(scoped_db.get_table_info([name]))
                    described_names.add(name)
        context = "Connection default schema (metadata only): " + json.dumps(default_schema) + "\n\n" + "\n\n".join(contexts)
        log_event("context.build.completed", context_bytes=len(context.encode()), sample_rows=0)
        if len(context.encode()) > MAX_CONTEXT_BYTES:
            raise GenerationError("Selected schema exceeds the context budget; narrow the request.")
        check_budget()
        sql_started = time.monotonic()
        log_event("sql.generation.started", stage="sql_generation", provider="deepseek")
        sql = create_sql_query_chain(self.model | RunnableLambda(complete_message),
                                     RequestDatabase(self.engine, context), prompt=SQL_PROMPT).invoke({"question": question})
        check_budget()
        log_event("sql.generation.completed", elapsed_ms=round((time.monotonic() - sql_started) * 1000), sql_bytes=len(sql.encode()))
        dialect = DIALECTS.get(self.engine.dialect.name, {}).get("parser")
        if dialect is None:
            raise GenerationError("Unsupported database dialect; consult the compatibility matrix.")
        log_event("sql.validation.started", stage="sql_validation")
        sql = validate_sql(sql, dialect, metadata, single_schema, default_schema=default_schema)
        log_event("sql.validation.completed", execution_performed=False)
        return {
            "status": "generated", "sql": sql, "dialect": self.engine.dialect.name,
            "schema": single_schema, "schemas": sorted({scope for scope, _ in selected_objects}),
            "default_schema": default_schema,
            "tables": [f"{scope}.{name}" for scope, name in selected_objects],
            "execution_performed": False,
            "warnings": ["Business meaning, database execution and query cost are not verified."],
        }
