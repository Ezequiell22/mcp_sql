"""Conservative PostgreSQL semantic checks beyond parser acceptance.

This is intentionally a supported subset, not PostgreSQL's server analyzer.
Unknown overloads must not become a promise that a statement will execute.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlglot import exp

from .domain import SqlError

NUMERIC = {"smallint", "integer", "bigint", "numeric", "real", "double precision"}
TEXT = {"text", "character varying", "character"}


def fail(message):
    raise SqlError("SQL_TYPE_ERROR", message, "sql")


def compatible(left: str, right: str) -> bool:
    return left == right or {left, right} <= NUMERIC or {left, right} <= TEXT


def check_literal(value: str, datatype: str) -> None:
    try:
        if datatype in NUMERIC:
            n = Decimal(value)
            if not n.is_finite():
                fail("Nonfinite numeric literals are unsupported.")
            if datatype in {"smallint", "integer", "bigint"}:
                if n != n.to_integral_value():
                    fail("Integer literal is not integral.")
                bounds = {
                    "smallint": (-32768, 32767),
                    "integer": (-2147483648, 2147483647),
                    "bigint": (-9223372036854775808, 9223372036854775807),
                }
                if not bounds[datatype][0] <= n <= bounds[datatype][1]:
                    fail("Integer literal is out of range.")
        elif datatype == "date":
            date.fromisoformat(value)
        elif datatype.startswith("timestamp"):
            datetime.fromisoformat(value)
        elif datatype == "uuid":
            UUID(value)
        elif datatype == "boolean" and value.lower() not in {
            "true",
            "false",
            "yes",
            "no",
            "on",
            "off",
            "1",
            "0",
            "t",
            "f",
            "y",
            "n",
        }:
            fail("Boolean literal is invalid.")
    except (ValueError, InvalidOperation):
        fail("Literal is incompatible with its PostgreSQL context.")


def check(query: exp.Expr, normalize_type) -> None:
    def datatype(node):
        return (
            normalize_type(node.type.sql(dialect="postgres"))
            if node is not None and node.type
            else "unknown"
        )

    text_only = (
        exp.Upper,
        exp.Lower,
        exp.Initcap,
        exp.Substring,
        exp.Trim,
        exp.Left,
        exp.Right,
        exp.Reverse,
        exp.Length,
    )
    numeric_only = (exp.Abs, exp.Ceil, exp.Floor, exp.Round, exp.Sqrt, exp.Pow, exp.Ln, exp.Exp)
    for node in query.walk():
        if (
            isinstance(node, text_only)
            and datatype(node.this) not in TEXT
            and not isinstance(node.this, exp.Literal)
        ):
            fail("Text function requires a text argument or explicit cast.")
        if isinstance(node, numeric_only) and datatype(node.this) not in NUMERIC:
            fail("Numeric function requires a numeric argument.")
        if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal):
            check_literal(node.this.this, datatype(node))
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            for literal, other in [(node.this, node.expression), (node.expression, node.this)]:
                if (
                    isinstance(literal, exp.Literal)
                    and literal.is_string
                    and datatype(other) not in TEXT
                ):
                    check_literal(literal.this, datatype(other))
        if isinstance(node, exp.SetOperation):
            left, right = node.this.selects, node.expression.selects
            if len(left) != len(right):
                fail("Set-operation output counts disagree.")
            for a, b in zip(left, right, strict=True):
                if not compatible(datatype(a), datatype(b)):
                    fail("Set-operation output types disagree; cast explicitly.")
        if isinstance(node, exp.Window):
            # Nested window functions are never legal at the same query level.
            if node.this.find(exp.Window):
                fail("Nested window functions are unsupported.")
        if isinstance(node, exp.AggFunc):
            parent = node.find_ancestor(exp.AggFunc)
            if parent and not parent.find_ancestor(exp.Window):
                fail("Nested aggregate requires a separate query level.")
        if isinstance(node, (exp.Where, exp.Having, exp.Join)):
            condition = (
                node.this if isinstance(node, (exp.Where, exp.Having)) else node.args.get("on")
            )
            if condition is not None and datatype(condition) != "boolean":
                fail("Predicate must resolve to boolean.")
        if isinstance(node, (exp.And, exp.Or)):
            if any(datatype(x) != "boolean" for x in (node.this, node.expression)):
                fail("Boolean operators require boolean operands.")
        if isinstance(node, exp.Not) and datatype(node.this) != "boolean":
            fail("NOT requires a boolean operand.")
        if isinstance(node, (exp.Where, exp.Having)):
            for window in node.find_all(exp.Window):
                if window.find_ancestor(exp.Select) is node.find_ancestor(exp.Select):
                    fail("Window functions are not allowed in WHERE/HAVING.")
