"""Validate one SELECT, execute unchanged, return every row, and roll back."""

import base64
import math
import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import sqlglot
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from MCP.settings import Settings


class QueryError(Exception):
    """Application-owned error safe to expose over MCP."""


# Unknown or qualified routines fail closed: SELECT alone does not imply no effects.
READ_FUNCTIONS = frozenset(
    """
ABS ACOS ASIN ATAN ATAN2 AVG CEIL CEILING FLOOR ROUND TRUNC POW POWER SQRT SIGN MOD
EXP LN LOG LOG10 GREATEST LEAST SUM MIN MAX COUNT COALESCE NULLIF ISNULL
CASE CAST TRY_CAST CONVERT EXTRACT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
DATE DATE_ADD DATE_SUB DATE_DIFF DATE_TRUNC DATE_PART TIMESTAMP TIMESTAMP_TRUNC TIME INTERVAL
YEAR QUARTER MONTH DAY DAY_OF_MONTH DAY_OF_WEEK DAY_OF_YEAR WEEK WEEK_OF_YEAR
HOUR MINUTE SECOND LAST_DAY DATEDIFF LOWER UPPER LENGTH CHAR_LENGTH
CHARACTER_LENGTH OCTET_LENGTH TRIM LTRIM RTRIM SUBSTRING LEFT RIGHT REPLACE
CONCAT CONCAT_WS SPLIT_PART STR_POSITION POSITION STARTSWITH ENDSWITH LIKE ILIKE
REGEXP_LIKE REGEXP_REPLACE REGEXP_EXTRACT REPEAT REVERSE LPAD RPAD ASCII CHR CHAR
UNICODE SPACE ROW_NUMBER RANK DENSE_RANK NTILE LAG LEAD FIRST_VALUE LAST_VALUE
NTH_VALUE PERCENT_RANK CUME_DIST PERCENTILE_CONT PERCENTILE_DISC STDDEV STDDEV_POP
STDDEV_SAMP VARIANCE VAR_POP VAR_SAMP ARRAY_AGG STRING_AGG BOOL_AND BOOL_OR
EVERY ANY_VALUE ARRAY_SIZE JSON_EXTRACT JSON_EXTRACT_SCALAR JSON_VALUE JSON_QUERY
JSON_ARRAY_LENGTH FILTER WITHIN_GROUP EXISTS
""".split()  # noqa: SIM905 - keep the built-in list compact and reviewable.
)
ANONYMOUS_READ_FUNCTIONS = {
    "postgres": {"age", "to_char", "to_date", "to_timestamp"},
    "tsql": {
        "datediff_big",
        "eomonth",
        "datefromparts",
        "datetimefromparts",
        "isnumeric",
        "len",
    },
}


def validate_sql(sql: str, dialect: str) -> str:
    """Structural filter, not a replacement for restricted database permissions."""
    try:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError
        statements = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
        if len(statements) != 1 or not isinstance(
            statements[0], (exp.Select, exp.SetOperation)
        ):
            raise ValueError
        for node in statements[0].walk():
            if isinstance(node, exp.Select) and not node.expressions:
                raise ValueError
            if isinstance(
                node,
                (
                    exp.DDL,
                    exp.DML,
                    exp.Command,
                    exp.Into,
                    exp.Lock,
                    exp.Parameter,
                    exp.Placeholder,
                    exp.Hint,
                    exp.WithTableHint,
                    exp.QueryOption,
                    exp.NextValueFor,
                ),
            ):
                raise TypeError
            if isinstance(node, exp.Table) and (
                node.catalog or not isinstance(node.this, exp.Identifier)
            ):
                raise ValueError
            if isinstance(node, exp.Func):
                if isinstance(node.parent, exp.Dot):
                    raise TypeError
                if isinstance(node, exp.Anonymous):
                    if node.name.casefold() not in ANONYMOUS_READ_FUNCTIONS[dialect]:
                        raise ValueError
                elif node.sql_name() not in READ_FUNCTIONS:
                    raise ValueError
        # Do not regenerate SQL: preserve native syntax, literals and explicit limits.
        return sql
    except Exception:  # noqa: BLE001 - sanitize parser/driver errors at the public boundary.
        raise QueryError(
            "Provide exactly one read-only SELECT; unsupported SQL is rejected."
        ) from None


def json_value(value):
    """Preserve decimal precision and represent DB values explicitly in JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID, timedelta)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    raise QueryError("The result contains an unsupported database value type.")


class Executor:
    def __init__(self, settings: Settings):
        from sqlalchemy.engine import make_url

        backend = make_url(settings.database_url).get_backend_name()
        self.dialect = {"postgresql": "postgres", "mssql": "tsql"}[backend]
        timeout = settings.query_timeout_seconds
        if backend == "postgresql":
            connect_args = {
                "connect_timeout": timeout,
                "autocommit": False,
                "options": f"-c default_transaction_read_only=on -c statement_timeout={timeout * 1000}",
            }
        else:
            import pyodbc

            # Avoid driver-level pooling retaining state after SQLAlchemy closes.
            pyodbc.pooling = False
            connect_args = {"timeout": timeout, "autocommit": False}
        self.engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            poolclass=NullPool,
            hide_parameters=True,
            echo=False,
        )
        if backend == "mssql":

            @event.listens_for(self.engine, "connect")
            def set_query_timeout(connection, record):
                connection.timeout = timeout

        self.lock = threading.Lock()

    def execute(self, sql: str) -> dict:
        sql = validate_sql(sql, self.dialect)
        if not self.lock.acquire(blocking=False):
            raise QueryError(
                "The server is busy; retry after the current query completes."
            )
        try:
            # A fresh connection per call; no commit, session state or result cursor survives.
            with self.engine.connect() as connection, connection.begin() as transaction:
                if self.dialect == "postgres":
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                with connection.execution_options(no_parameters=True).exec_driver_sql(
                    sql
                ) as result:
                    columns = list(result.keys())
                    rows = [[json_value(value) for value in row] for row in result]
                transaction.rollback()
            return {"columns": columns, "rows": rows, "row_count": len(rows)}
        except QueryError:
            raise
        except Exception:  # noqa: BLE001 - sanitize parser/driver errors at the public boundary.
            raise QueryError(
                "Query failed or timed out; check SQL, reader permissions and database availability."
            ) from None
        finally:
            self.lock.release()
