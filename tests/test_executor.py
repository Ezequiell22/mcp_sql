import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import create_engine

from MCP.executor import Executor, QueryError, json_value, validate_sql
from MCP.settings import Settings


@pytest.mark.parametrize(
    "dialect,sql",
    [
        ("postgres", "SELECT * FROM public.orders WHERE name LIKE '%Smith%'"),
        ("postgres", "WITH a AS (SELECT 1 AS id) SELECT * FROM a UNION ALL SELECT 2"),
        (
            "postgres",
            "SELECT COUNT(*), SUM(amount), DATE_TRUNC('month', CURRENT_TIMESTAMP) FROM orders",
        ),
        ("postgres", "SELECT '; DROP TABLE x' AS literal /* harmless ; */;"),
        ("postgres", "SELECT id FROM orders LIMIT 3 OFFSET 2"),
        ("tsql", "SELECT TOP (3) [id] FROM dbo.orders ORDER BY id"),
        ("tsql", "SELECT DATEADD(day, 1, GETDATE()), LEN('abc')"),
        (
            "tsql",
            "WITH a AS (SELECT 1 AS id) SELECT ROW_NUMBER() OVER (ORDER BY id) FROM a",
        ),
    ],
)
def test_accepts_without_rewriting(dialect, sql):
    assert validate_sql(sql, dialect) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "",
        " ",
        "SELECT",
        "SELECT 1; SELECT 2",
        "SELECT 1; -- comment\nDROP TABLE t",
        "DELETE FROM t",
        "UPDATE t SET id=2",
        "INSERT INTO t SELECT 1",
        "CREATE TABLE t(id int)",
        "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
        "SELECT * INTO copy FROM t",
        "SELECT * FROM t FOR UPDATE",
        "SELECT pg_sleep(10)",
        "SELECT nextval('s')",
        "SELECT set_config('a','b',false)",
        "SELECT public.unsafe_function()",
        "SELECT * FROM other.public.t",
        "SELECT * FROM t WHERE id = :id",
        "SELECT * FROM t WHERE",
        "SELECT * FROM t ignored garbage",
        "SELECT NEXT VALUE FOR dbo.s",
        "SELECT * FROM t WITH (NOLOCK)",
        "SELECT * FROM OPENQUERY(remote, 'SELECT 1')",
        "EXEC dbo.proc",
        "SELECT @value = id FROM t",
    ],
)
@pytest.mark.parametrize("dialect", ["postgres", "tsql"])
def test_guard_rejects(sql, dialect):
    with pytest.raises(QueryError):
        validate_sql(sql, dialect)


def test_execution_returns_every_row_duplicate_columns_and_empty_result():
    executor = Executor.__new__(Executor)
    executor.dialect = "postgres"
    import threading

    executor.lock = threading.Lock()
    executor.engine = create_engine("sqlite://")
    # SQLite fixtures exercise row collection; mock only PostgreSQL's SET command.
    from sqlalchemy import event

    @event.listens_for(executor.engine, "before_cursor_execute", retval=True)
    def adapt(connection, cursor, sql, parameters, context, many):
        return (
            ("SELECT 1", ())
            if sql == "SET TRANSACTION READ ONLY"
            else (sql, parameters)
        )

    sql = "WITH RECURSIVE a(id) AS (SELECT 1 UNION ALL SELECT id+1 FROM a WHERE id<1500) SELECT id AS n, id AS n FROM a"
    try:
        result = executor.execute(sql)
        assert result["columns"] == ["n", "n"]
        assert result["row_count"] == 1500
        assert result["rows"][-1] == [1500, 1500]
        assert executor.execute("SELECT 1 AS n WHERE 1=0") == {
            "columns": ["n"],
            "rows": [],
            "row_count": 0,
        }
        with executor.lock, pytest.raises(QueryError, match="busy"):
            executor.execute("SELECT 1")
        with pytest.raises(QueryError, match="failed or timed out") as error:
            executor.execute("SELECT private_secret FROM missing")
        assert "private_secret" not in str(error.value)
        assert executor.execute("SELECT 1")["rows"] == [[1]]
    finally:
        executor.engine.dispose()


def test_conversion_is_json_safe_and_precision_preserving():
    values = [
        None,
        True,
        1,
        1.5,
        float("nan"),
        Decimal("123456789.0123456789"),
        date(2026, 11, 6),
        time(12, 30),
        datetime(2026, 11, 6, 12, 30),
        UUID(int=0),
        b"\x00\xff",
        {"x": [Decimal("1.20")]},
    ]
    result = json_value(values)
    json.dumps(result, allow_nan=False)
    assert result[5] == "123456789.0123456789"
    assert result[10] == {"base64": "AP8="}
    assert result[11] == {"x": ["1.20"]}
    assert json_value(timedelta(days=1)) == "1 day, 0:00:00"
    with pytest.raises(QueryError, match="unsupported"):
        json_value(object())


@pytest.mark.parametrize("backend", ["postgresql+psycopg", "mssql+pyodbc"])
def test_driver_timeouts_and_fresh_connections(backend):
    with (
        patch("MCP.executor.create_engine") as factory,
        patch("MCP.executor.event.listens_for") as listen,
    ):
        listen.return_value = lambda callback: callback(MagicMock(), None)
        executor = Executor(Settings(f"{backend}://reader:secret@localhost/db", 7))
        args = factory.call_args.kwargs
        assert args["poolclass"].__name__ == "NullPool"
        if backend.startswith("postgresql"):
            assert args["connect_args"] == {
                "connect_timeout": 7,
                "autocommit": False,
                "options": "-c default_transaction_read_only=on -c statement_timeout=7000",
            }
        else:
            assert args["connect_args"] == {"timeout": 7, "autocommit": False}
        executor.engine.dispose()


@pytest.mark.parametrize(
    "dialect,sql",
    [
        ("postgres", "SELECT * FROM t WHERE id=$1"),
        ("tsql", "SELECT * FROM t OPTION (RECOMPILE)"),
    ],
)
def test_dialect_specific_rejections(dialect, sql):
    with pytest.raises(QueryError):
        validate_sql(sql, dialect)
