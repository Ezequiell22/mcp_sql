"""Conservative T-SQL static analysis; never transpiles or executes the returned SQL.

SQLGlot is not SQL Server's native compiler. Capabilities and evidence explicitly
limit this adapter to a static subset; unknown syntax/type rules fail closed.
"""

import re
from datetime import datetime
from decimal import Decimal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

from .analyzer import MAX_DEPTH, MAX_NODES, MAX_SQL_BYTES, _check_scopes
from .bindings import _default, _fail
from .domain import Catalog, SqlError

SESSION_SETTINGS = {
    "QUOTED_IDENTIFIER": "ON",
    "ANSI_NULLS": "ON",
    "ANSI_WARNINGS": "ON",
    "ARITHABORT": "ON",
}

FUNCTIONS = frozenset(
    "count count_big sum avg min max row_number rank dense_rank nullif coalesce".split()
)
INTEGERS = {
    "tinyint": (0, 255),
    "smallint": (-32768, 32767),
    "int": (-2147483648, 2147483647),
    "bigint": (-9223372036854775808, 9223372036854775807),
}
NODES = frozenset(
    """
Select From Table TableAlias Identifier Column Alias Where Having Group Order Ordered Case If Coalesce
Join With CTE Subquery Exists Distinct Window WindowSpec RowNumber Rank DenseRank
Sum Avg Min Max Count Cast Nullif Parameter Var DataType DataTypeParam Literal National
Null Star Paren Neg Add Sub Mul Div EQ NEQ LT LTE GT GTE And Or Not Is In Between
Limit LimitOptions
""".split()
)


def normalize_type(value: str) -> str:
    value = re.sub(r"\s+", "", value.lower())
    value = re.sub(r"^integer$", "int", value)
    value = re.sub(r"^numeric(?=\()", "decimal", value)
    if value in INTEGERS or value in {"date", "bit", "uniqueidentifier"}:
        return value
    if match := re.fullmatch(r"decimal\((\d+),(\d+)\)", value):
        p, s = map(int, match.groups())
        if 1 <= p <= 38 and 0 <= s <= p:
            return f"decimal({p},{s})"
    if match := re.fullmatch(r"(n?varchar|n?char)\((\d+)\)", value):
        base, size = match.groups()
        if 1 <= int(size) <= (4000 if base.startswith("n") else 8000):
            return f"{base}({int(size)})"
    if match := re.fullmatch(r"(datetime2|datetimeoffset)(?:\(([0-7])\))?", value):
        return f"{match[1]}({match[2] or '7'})"
    raise SqlError(
        "UNSUPPORTED_SQL_FEATURE", "T-SQL type is outside the supported exact-type subset.", "sql"
    )


def logical_type(value: str) -> str:
    value = normalize_type(value)
    if value in INTEGERS:
        return "integer"
    if value.startswith("decimal("):
        return "decimal"
    if value == "bit":
        return "boolean"
    if value == "date":
        return "date"
    if value.startswith("datetime"):
        return "timestamp"
    return "text"


def check_default(field: dict, datatype: str) -> None:
    kind = logical_type(datatype)
    surrogate = {
        "int": "integer",
        "tinyint": "integer",
        "bit": "boolean",
        "uniqueidentifier": "uuid",
    }.get(datatype, datatype)
    if datatype.startswith("decimal("):
        surrogate = "numeric"
    elif datatype.startswith("datetime"):
        surrogate = (
            "timestamp with time zone"
            if datatype.startswith("datetimeoffset")
            else "timestamp without time zone"
        )
    elif kind == "text" and datatype != "uniqueidentifier":
        surrogate = "text"
    _default(field, surrogate)
    value = field.get("default")
    if value is None:
        return
    if (
        datatype in INTEGERS
        and not INTEGERS[datatype][0] <= Decimal(value) <= INTEGERS[datatype][1]
    ):
        _fail("Default exceeds its SQL Server integer type.")
    if datatype.startswith("decimal("):
        p, s = map(int, re.findall(r"\d+", datatype))
        number = Decimal(value)
        if abs(number) >= Decimal(10) ** (p - s) or max(0, -number.as_tuple().exponent) > s:
            _fail("Default exceeds its SQL Server decimal precision or scale.")
    if datatype.startswith("datetime"):
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|[+-]\d{2}:\d{2})?", value
        ):
            _fail(
                "SQL Server timestamp defaults require an ISO date, time and optional exact fractional seconds/offset."
            )
        precision = int(re.search(r"\((\d+)\)", datatype)[1])
        fractional = re.search(r"[T ]\d{2}:\d{2}:\d{2}\.(\d+)", value)
        if fractional and len(fractional[1]) > precision:
            _fail("Timestamp default exceeds its SQL Server fractional precision.")
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is not None and (
            abs(parsed.utcoffset().total_seconds()) > 14 * 3600
            or parsed.utcoffset().total_seconds() % 60
        ):
            _fail("SQL Server timezone offsets require whole minutes within 14 hours.")
    if "char(" in datatype:
        length = int(re.search(r"\d+", datatype)[0])
        # No database collation/codepage assumption: non-Unicode defaults are ASCII only.
        if datatype.startswith("n"):
            actual = len(value.encode("utf-16-le")) // 2
        else:
            if not value.isascii():
                _fail("Non-Unicode defaults must be ASCII; use nvarchar for Unicode.")
            actual = len(value)
        if actual > length:
            _fail("Default exceeds its SQL Server text length.")


def _unsupported(message):
    raise SqlError("UNSUPPORTED_SQL_FEATURE", message, "sql")


def _type_error(message):
    raise SqlError("SQL_TYPE_ERROR", message, "sql")


def _cast_type(node):
    return normalize_type(node.sql(dialect="tsql"))


def _numeric(t):
    return t in INTEGERS or t.startswith("decimal(")


def _compatible(a, b):
    return (
        a == b
        or a == "null"
        or b == "null"
        or (_numeric(a) and _numeric(b))
        or ("char(" in a and "char(" in b)
    )


def _common_type(types):
    types = [t for t in types if t != "null"]
    if not types:
        return "null"
    if len(set(types)) == 1:
        return types[0]
    if all(t in INTEGERS for t in types):
        return max(types, key=lambda t: list(INTEGERS).index(t))
    if all(_numeric(t) for t in types):
        parts = [
            tuple(map(int, re.findall(r"\d+", t)))
            if t.startswith("decimal(")
            else ({"tinyint": 3, "smallint": 5, "int": 10, "bigint": 19}[t], 0)
            for t in types
        ]
        scale = max(s for p, s in parts)
        precision = scale + max(p - s for p, s in parts)
        if precision > 38:
            _unsupported(
                "Conditional numeric branches require decimal scale reduction; cast branches to a common exact type."
            )
        return f"decimal({precision},{scale})"
    _type_error("Conditional branches require a common supported type; use explicit casts.")


def _arithmetic(node, a, b):
    if not (_numeric(a) and _numeric(b)):
        _type_error("Arithmetic requires supported numeric operands.")
    if isinstance(node, exp.Div) and not isinstance(node.expression, exp.Nullif):
        _type_error("Division requires an explicit NULLIF denominator.")
    if a in INTEGERS and b in INTEGERS:
        return "bigint" if "bigint" in (a, b) else "int"

    def precision(t):
        return (
            (int(re.findall(r"\d+", t)[0]), int(re.findall(r"\d+", t)[1]))
            if t.startswith("decimal(")
            else ({"tinyint": 3, "smallint": 5, "int": 10, "bigint": 19}[t], 0)
        )

    p1, s1 = precision(a)
    p2, s2 = precision(b)
    if isinstance(node, (exp.Add, exp.Sub)):
        scale = max(s1, s2)
        precision = scale + max(p1 - s1, p2 - s2) + 1
    elif isinstance(node, exp.Mul):
        precision, scale = p1 + p2 + 1, s1 + s2
    else:
        scale = max(6, s1 + p2 + 1)
        precision = p1 - s1 + s2 + scale
    # SQL Server's >38 scale reduction has special cases. Reject until conformance
    # tests cover them, rather than publishing an approximate binding type.
    if precision > 38:
        _unsupported(
            "Decimal arithmetic requiring scale reduction is unsupported; choose explicit smaller operand casts."
        )
    return f"decimal({precision},{scale})"


class SqlServerAnalyzer:
    def analyze(self, sql: str, catalog: Catalog) -> dict:
        if catalog.dialect != "sqlserver" or catalog.database_major not in {16, 17}:
            raise SqlError(
                "UNSUPPORTED_DATABASE_VERSION",
                "SQL Server analyzer targets major versions 16 and 17.",
            )
        if len(sql.encode()) > MAX_SQL_BYTES:
            raise SqlError("INPUT_TOO_LARGE", "SQL exceeds 256 KiB.", "sql")
        try:
            return self._analyze(sql, catalog)
        except SqlError:
            raise
        except (SqlglotError, ValueError, TypeError, KeyError, RecursionError, AttributeError):
            _unsupported(
                "T-SQL resolution could not establish a supported query; inspect the published capabilities."
            )

    def _analyze(self, sql, catalog):
        try:
            parsed = sqlglot.parse(sql, read="tsql", error_level=ErrorLevel.RAISE)
        except SqlglotError:
            raise SqlError(
                "SQL_PARSE_ERROR", "T-SQL parser rejected the statement.", "sql"
            ) from None
        # Include empty/comment-only statements in the count: batches never pass.
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
            raise SqlError(
                "READ_ONLY_VIOLATION", "Exactly one analytical SELECT is required.", "sql"
            )
        query = parsed[0]
        # SQLGlot accepts some foreign syntax; reject known non-T-SQL tokens.
        for token in sqlglot.Dialect.get_or_raise("tsql").tokenize(sql):
            if token.token_type.name in {"DCOLON", "LIMIT", "FETCH", "OFFSET"}:
                _unsupported("Use T-SQL CAST and TOP syntax; LIMIT/OFFSET/FETCH are not supported.")
        functions, params = set(), {}
        stack = [(query, 0)]
        count = 0
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > MAX_NODES or depth > MAX_DEPTH:
                raise SqlError("INPUT_TOO_LARGE", "SQL exceeds the AST budget.", "sql")
            if type(node).__name__ not in NODES:
                _unsupported(
                    f"T-SQL construct {type(node).__name__} is outside the supported policy."
                )
            stack.extend((child, depth + 1) for child in node.iter_expressions())
            if isinstance(node, exp.Select):
                allowed = {
                    "expressions",
                    "from_",
                    "joins",
                    "where",
                    "having",
                    "group",
                    "order",
                    "with_",
                    "distinct",
                    "limit",
                }
                if any(value and key not in allowed for key, value in node.args.items()):
                    _unsupported(
                        "SELECT modifiers, writes, hints and query options are unsupported."
                    )
                for p in node.selects:
                    if not isinstance(p, (exp.Alias, exp.Column)) or not p.alias_or_name:
                        _unsupported("Every calculation requires an explicit output alias.")
                if node.args.get("limit"):
                    limit = node.args["limit"]
                    if (
                        not isinstance(limit.expression, exp.Literal)
                        or not limit.expression.is_int
                        or int(limit.expression.this) < 1
                        or limit.args.get("limit_options")
                    ):
                        _unsupported(
                            "TOP requires a positive integer without PERCENT or WITH TIES."
                        )
                    if not node.args.get("order"):
                        _unsupported("TOP requires an explicit ORDER BY.")
            if isinstance(node, exp.With) and node.args.get("recursive"):
                _unsupported("Recursive CTEs are unsupported by the T-SQL adapter.")
            if isinstance(node, exp.Table) and (
                not isinstance(node.this, exp.Identifier)
                or any(v and k not in {"this", "db", "alias"} for k, v in node.args.items())
            ):
                _unsupported(
                    "Table functions, hints, temporal and cross-database references are unsupported."
                )
            if isinstance(node, exp.Column) and (node.catalog or node.db):
                _unsupported("Use an alias.column reference within the selected database.")
            if isinstance(node, exp.Var) and not isinstance(node.parent, exp.Parameter):
                _unsupported("Only declared @pN runtime parameters are supported.")
            if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
                _unsupported("Projection wildcards are forbidden.")
            if isinstance(node, exp.Parameter):
                if (
                    not re.fullmatch(r"p[1-9]\d{0,2}|p1000", node.name)
                    or not isinstance(node.parent, exp.Cast)
                    or node.parent.this is not node
                ):
                    raise SqlError(
                        "PARAMETER_MISMATCH",
                        "Use explicitly cast @p1 through @p1000 parameters.",
                        "parameters",
                    )
                pos = int(node.name[1:])
                datatype = _cast_type(node.parent.to)
                if pos in params and params[pos] != datatype:
                    raise SqlError(
                        "PARAMETER_MISMATCH", "Repeated parameter casts must agree.", "parameters"
                    )
                params[pos] = datatype
            if isinstance(node, exp.DataType):
                _cast_type(node)
            if isinstance(
                node,
                (
                    exp.Sum,
                    exp.Avg,
                    exp.Min,
                    exp.Max,
                    exp.Count,
                    exp.RowNumber,
                    exp.Rank,
                    exp.DenseRank,
                ),
            ):
                name = (
                    "count_big"
                    if isinstance(node, exp.Count) and node.args.get("big_int")
                    else node.sql_name().lower()
                )
                if name not in FUNCTIONS:
                    _unsupported("Function is not approved for T-SQL.")
                functions.add(name)
            if isinstance(node, exp.Join) and node.args.get("kind") not in {None, "", "INNER"}:
                _unsupported("Only explicit inner/outer JOIN ON is supported.")
        if params and set(params) != set(range(1, max(params) + 1)):
            raise SqlError(
                "PARAMETER_MISMATCH", "Parameter positions must be contiguous.", "parameters"
            )

        schema_data = {}
        for relation in catalog.relations:
            cols = {}
            for col in relation.columns:
                if col.type_schema != "sys":
                    continue
                try:
                    cols[col.name] = normalize_type(col.postgres_type)
                except SqlError:
                    continue
            schema_data.setdefault(relation.schema, {})[relation.name] = cols
        schema = MappingSchema(schema_data, dialect="tsql", normalize=False)
        for scope in traverse_scope(query):
            for _, (_, source) in scope.selected_sources.items():
                if isinstance(source, exp.Table):
                    relation = catalog.relation_map.get(f"{source.db}.{source.name}")
                    if (
                        not source.db
                        or not relation
                        or relation.kind != "table"
                        or source.alias_column_names
                    ):
                        raise SqlError(
                            "OBJECT_NOT_AUTHORIZED",
                            "Base tables require an authorized explicit schema.",
                            "sql",
                        )
        query = qualify(
            query, dialect="tsql", schema=schema, expand_stars=False, validate_qualify_columns=True
        )
        scopes = list(traverse_scope(query))
        if (
            sum(
                isinstance(s, exp.Table)
                for scope in scopes
                for _, (_, s) in scope.selected_sources.items()
            )
            > 1000
        ):
            raise SqlError("INPUT_TOO_LARGE", "Relation reference budget exceeded.", "sql")
        warnings = _check_scopes(scopes, catalog)
        resolved = {}
        dependencies, relations = set(), set()

        def infer(node, scope):
            key = id(node)
            if key in resolved:
                return resolved[key]
            origins = set()
            if isinstance(node, exp.Column):
                search = scope
                while search and node.table not in search.sources:
                    search = search.parent
                source = search.sources.get(node.table) if search else None
                if isinstance(source, exp.Table):
                    relation = catalog.relation_map.get(f"{source.db}.{source.name}")
                    col = relation.column_map.get(node.name) if relation else None
                    if col is None or col.type_schema != "sys":
                        raise SqlError("OBJECT_NOT_AUTHORIZED", "A column is unavailable.", "sql")
                    datatype = normalize_type(col.postgres_type)
                    origins.add(f"{relation.key}.{col.name}")
                    dependencies.update(origins)
                elif isinstance(source, Scope):
                    match = [p for p in source.expression.selects if p.alias_or_name == node.name]
                    if len(match) != 1:
                        _unsupported("Derived column could not be resolved uniquely.")
                    datatype, origins = infer(match[0], source)
                else:
                    raise SqlError("OBJECT_NOT_AUTHORIZED", "A column is unavailable.", "sql")
            elif isinstance(node, exp.Parameter):
                datatype = params[int(node.name[1:])]
            elif isinstance(node, exp.Null):
                datatype = "null"
            elif isinstance(node, (exp.Literal, exp.National)):
                if isinstance(node, exp.National) or node.is_string:
                    if not node.this.isascii() and not isinstance(node, exp.National):
                        _unsupported("Use N-prefixed Unicode literals.")
                    length = max(
                        1,
                        len(node.this.encode("utf-16-le")) // 2
                        if isinstance(node, exp.National)
                        else len(node.this),
                    )
                    datatype = normalize_type(
                        f"{'nvarchar' if isinstance(node, exp.National) else 'varchar'}({length})"
                    )
                elif node.is_int and 0 <= int(node.this) <= 2147483647:
                    datatype = "int"
                else:
                    if not re.fullmatch(r"\d+(?:\.\d+)?", node.this):
                        _unsupported("Only exact decimal literals are supported.")
                    whole, _, frac = node.this.partition(".")
                    datatype = normalize_type(
                        f"decimal({max(1, len(whole.lstrip('0')) + len(frac))},{len(frac)})"
                    )
            elif isinstance(node, exp.Star):
                datatype = "int"
            elif isinstance(node, exp.Distinct):
                if len(node.expressions) != 1:
                    _unsupported("Aggregate DISTINCT requires one expression.")
                datatype, origins = infer(node.expressions[0], scope)
            elif isinstance(node, exp.Cast):
                source_type, origins = infer(node.this, scope)
                datatype = _cast_type(node.to)
                if source_type != "null" and source_type != datatype:

                    def temporal(t):
                        return t == "date" or t.startswith("datetime")

                    def text(t):
                        return "char(" in t

                    def numeric(t):
                        return _numeric(t) or t == "bit"

                    if not (
                        text(source_type)
                        or text(datatype)
                        or (numeric(source_type) and numeric(datatype))
                        or (temporal(source_type) and temporal(datatype))
                    ):
                        _type_error(
                            "Explicit cast conversion is outside the supported T-SQL type rules."
                        )
                functions.add("cast")
            elif isinstance(node, (exp.Alias, exp.Paren, exp.Neg)):
                datatype, origins = infer(node.this, scope)
                if isinstance(node, exp.Neg) and (not _numeric(datatype) or datatype == "tinyint"):
                    _type_error("Unary minus requires a numeric input.")
            elif isinstance(node, exp.Case):
                branches = []
                base = infer(node.this, scope)[0] if node.this is not None else None
                for branch in node.args.get("ifs", []):
                    if base is None:
                        predicate(branch.this, scope)
                    elif not _compatible(base, infer(branch.this, scope)[0]):
                        _type_error("Simple CASE comparisons require compatible types.")
                    datatype, extra = infer(branch.args["true"], scope)
                    branches.append(datatype)
                    origins |= extra
                if not branches:
                    _type_error("CASE requires at least one WHEN branch.")
                if node.args.get("default") is not None:
                    datatype, extra = infer(node.args["default"], scope)
                    branches.append(datatype)
                    origins |= extra
                for col in node.find_all(exp.Column):
                    _, extra = infer(col, scope)
                    origins |= extra
                datatype = _common_type(branches)
            elif isinstance(node, exp.Coalesce):
                values = [node.this, *node.expressions]
                if len(values) < 2:
                    _type_error("COALESCE requires at least two arguments.")
                branches = []
                for value in values:
                    datatype, extra = infer(value, scope)
                    branches.append(datatype)
                    origins |= extra
                datatype = _common_type(branches)
                functions.add("coalesce")
            elif isinstance(node, exp.Nullif):
                a, ao = infer(node.this, scope)
                b, bo = infer(node.expression, scope)
                if a == "null" or not _compatible(a, b):
                    _type_error(
                        "NULLIF operands are incompatible or its first argument is untyped NULL."
                    )
                datatype, origins = a, ao | bo
                functions.add("nullif")
            elif isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
                a, ao = infer(node.this, scope)
                b, bo = infer(node.expression, scope)
                datatype, origins = _arithmetic(node, a, b), ao | bo
            elif isinstance(node, (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count)):
                if getattr(node, "expressions", []):
                    _type_error("Aggregate requires exactly one argument.")
                if node.this is None:
                    _type_error("Aggregate argument is required.")
                a, origins = infer(node.this, scope)
                if node.this.find(exp.AggFunc) or node.this.find(exp.Window):
                    _type_error("Nested aggregate/window inputs are unsupported.")
                if isinstance(node, exp.Count):
                    datatype = "bigint" if node.args.get("big_int") else "int"
                elif isinstance(node, (exp.Sum, exp.Avg)):
                    if not _numeric(a):
                        _type_error("SUM/AVG require a numeric input other than bit.")
                    if a.startswith("decimal("):
                        scale = int(re.findall(r"\d+", a)[1])
                        datatype = (
                            f"decimal(38,{max(scale, 6) if isinstance(node, exp.Avg) else scale})"
                        )
                    else:
                        datatype = "bigint" if a == "bigint" else "int"
                else:
                    if a == "bit":
                        _type_error("MIN/MAX do not accept bit.")
                    datatype = a
            elif isinstance(node, (exp.RowNumber, exp.Rank, exp.DenseRank)):
                window = node.parent
                if (
                    not isinstance(window, exp.Window)
                    or not window.args.get("order")
                    or window.args.get("spec")
                ):
                    _type_error("Ranking requires OVER with ORDER BY and no frame.")
                datatype = "bigint"
            elif isinstance(node, exp.Window):
                if not isinstance(node.this, (exp.RowNumber, exp.Rank, exp.DenseRank)):
                    _unsupported("Only ranking windows are enabled by the T-SQL adapter.")
                window_columns = list(node.args.get("partition_by") or [])
                if node.args.get("order"):
                    window_columns += [o.this for o in node.args["order"].expressions]
                if any(not isinstance(c, exp.Column) for c in window_columns):
                    _unsupported(
                        "Ranking ORDER/PARTITION expressions require direct columns; precompute calculations in a CTE."
                    )
                datatype, origins = infer(node.this, scope)
                for col in node.find_all(exp.Column):
                    _, extra = infer(col, scope)
                    origins |= extra
            else:
                _unsupported(
                    f"Output expression {type(node).__name__} has no supported T-SQL type rule."
                )
            resolved[key] = (datatype, origins)
            return datatype, origins

        def predicate(node, scope):
            if isinstance(node, (exp.And, exp.Or)):
                predicate(node.this, scope)
                predicate(node.expression, scope)
            elif isinstance(node, (exp.Not, exp.Paren)):
                predicate(node.this, scope)
            elif isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)):
                a, _ = infer(node.this, scope)
                b, _ = infer(node.expression, scope)
                if not _compatible(a, b):
                    _type_error("Comparison operands require compatible explicit types.")
            elif isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
                infer(node.this, scope)
            elif isinstance(node, exp.Between):
                a, _ = infer(node.this, scope)
                for bound in (node.args["low"], node.args["high"]):
                    b, _ = infer(bound, scope)
                    if not _compatible(a, b):
                        _type_error("BETWEEN operands require compatible explicit types.")
            elif isinstance(node, exp.In) and not node.args.get("query") and node.expressions:
                a, _ = infer(node.this, scope)
                for item in node.expressions:
                    b, _ = infer(item, scope)
                    if not _compatible(a, b):
                        _type_error("IN operands require compatible explicit types.")
            elif isinstance(node, exp.Exists) and isinstance(node.this, exp.Select):
                pass  # Each nested scope is independently resolved and checked.
            else:
                _type_error("Predicate must be a supported T-SQL boolean comparison.")
            if node.find(exp.Window):
                _type_error("Window functions are not valid in predicates.")

        for scope in scopes:
            for clause in ("where", "having"):
                part = scope.expression.args.get(clause)
                if part:
                    predicate(part.this, scope)
            for join in scope.expression.args.get("joins", []):
                if join.args.get("on"):
                    predicate(join.args["on"], scope)
            order = scope.expression.args.get("order")
            if order:
                for ordered in order.expressions:
                    if isinstance(ordered.this, exp.Column) and not ordered.this.table:
                        matching = [
                            p
                            for p in scope.expression.selects
                            if p.alias_or_name == ordered.this.name
                        ]
                        if len(matching) != 1 or not isinstance(matching[0], exp.Alias):
                            _unsupported("ORDER BY alias could not be resolved uniquely.")
                        ordered.set("this", matching[0].this.copy())
                    infer(ordered.this, scope)
            for _, (_, source) in scope.selected_sources.items():
                if isinstance(source, exp.Table):
                    relations.add(f"{source.db}.{source.name}")
            for col in scope.columns:
                infer(col, scope)
            for projection in scope.expression.selects:
                infer(projection, scope)
            # T-SQL does not implement PostgreSQL's GROUP BY primary-key dependency shortcut.
            group = scope.expression.args.get("group")
            has_aggregate = any(
                a.find_ancestor(exp.Select) is scope.expression and not a.find_ancestor(exp.Window)
                for a in scope.expression.find_all(exp.AggFunc)
            )
            if group:
                for expression in group.expressions:
                    infer(expression, scope)
            if group or has_aggregate:
                grouped = {g.sql() for g in group.expressions} if group else set()
                checked_expressions = list(scope.expression.selects)
                if scope.expression.args.get("having"):
                    checked_expressions.append(scope.expression.args["having"].this)
                if order:
                    checked_expressions.extend(o.this for o in order.expressions)
                for projection in checked_expressions:
                    expression = (
                        projection.this if isinstance(projection, exp.Alias) else projection
                    )
                    if expression.sql() in grouped:
                        continue
                    for col in expression.find_all(exp.Column):
                        if (
                            col.find_ancestor(exp.Select) is scope.expression
                            and not col.find_ancestor(exp.AggFunc)
                            and col.sql() not in grouped
                        ):
                            _type_error(
                                "T-SQL projected columns must be explicitly grouped or aggregated."
                            )
            for comparison in scope.expression.find_all(
                exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE
            ):
                if comparison.find_ancestor(exp.Select) is scope.expression:
                    a, _ = infer(comparison.this, scope)
                    b, _ = infer(comparison.expression, scope)
                    if not _compatible(a, b):
                        _type_error("Comparison operands require compatible explicit types.")

        predicates, expressions, ranges = {}, {}, {}
        for parameter in query.find_all(exp.Parameter):
            pos = int(parameter.name[1:])
            cast = parameter.parent
            comparison = cast.parent
            predicate = parameter.find_ancestor(exp.Where, exp.Having, exp.Join)
            if predicate is None or not isinstance(
                comparison, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
            ):
                raise SqlError(
                    "PARAMETER_MISMATCH",
                    "Every parameter must directly constrain a WHERE/HAVING/JOIN comparison.",
                    "parameters",
                )
            operator = comparison.key
            other = comparison.expression if comparison.this is cast else comparison.this
            if comparison.this is cast:
                operator = {"lt": "gt", "lte": "gte", "gt": "lt", "gte": "lte"}.get(
                    operator, operator
                )
            path, conjunctive = comparison.parent, True
            while path is not None and path is not predicate:
                conjunctive &= isinstance(path, (exp.And, exp.Paren))
                path = path.parent
            predicates.setdefault(pos, []).append(operator)
            expressions.setdefault(pos, []).append(
                other.sql() if other.find(exp.Column) or other.find(exp.AggFunc) else None
            )
            ranges.setdefault(pos, []).append(
                {"clause": id(predicate), "conjunctive": bool(conjunctive and path is predicate)}
            )
        outputs = []
        for projection in query.selects:
            datatype, origins = infer(projection, scopes[-1])
            if datatype == "null":
                _unsupported("NULL outputs require an explicit supported cast.")
            outputs.append(
                {
                    "key": projection.alias_or_name,
                    "sqlType": datatype,
                    "logicalType": logical_type(datatype),
                    "nullable": None,
                    "lineage": sorted(origins),
                }
            )
        if len({o["key"].casefold() for o in outputs}) != len(outputs):
            raise SqlError("OUTPUT_CONTRACT_MISMATCH", "Output names must be unique.", "outputs")
        return {
            "parameters": params,
            "parameterPredicates": predicates,
            "parameterExpressions": expressions,
            "parameterRanges": ranges,
            "outputs": outputs,
            "dependencies": {
                "relations": sorted(relations),
                "columns": sorted(dependencies),
                "functions": sorted(functions),
            },
            "warnings": warnings,
        }
