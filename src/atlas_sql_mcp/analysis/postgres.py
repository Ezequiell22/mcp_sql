"""PostgreSQL-native syntax/policy followed by scoped catalog and type analysis.

SQLGlot is an analysis aid, never an SQL rewriter or destination executor. The
original statement is returned only after both independent representations pass.
"""

import json

import sqlglot
from pglast import parser
from sqlglot import exp
from sqlglot.dialects import Dialect
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.lineage import lineage
from sqlglot.optimizer.annotate_types import TypeAnnotator
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

from atlas_sql_mcp.analysis import postgres_types as type_policy
from atlas_sql_mcp.analysis.limits import MAX_DEPTH, MAX_NODES, MAX_SQL_BYTES
from atlas_sql_mcp.analysis.postgres_types import TYPES, logical_type, normalize_type
from atlas_sql_mcp.analysis.scopes import check_scopes
from atlas_sql_mcp.domain import Catalog, SqlError

FUNCTIONS = frozenset(
    """
count sum avg min max bool_and bool_or every array_agg string_agg json_agg jsonb_agg
json_object_agg jsonb_object_agg stddev stddev_pop stddev_samp variance var_pop var_samp
row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value
abs ceil ceiling floor round trunc mod power sqrt cbrt sign exp ln log greatest least
lower upper initcap length char_length character_length octet_length btrim ltrim rtrim trim
substring substr replace translate concat concat_ws left right reverse split_part strpos position
regexp_replace regexp_match regexp_matches starts_with
coalesce nullif date_trunc date_part extract make_date make_timestamp make_timestamptz
age timezone to_char to_date to_timestamp isfinite
jsonb_extract_path jsonb_extract_path_text jsonb_typeof json_typeof jsonb_array_length json_array_length
array_length array_ndims cardinality array_position array_positions array_to_string
percentile_cont percentile_disc mode grouping
""".split()
)
# JSONPath execution, regclass/regproc casts, record/composite types and arbitrary
# extension types are deliberately absent. Only built-in scalar/array operators.
ARITIES = {
    **{
        name: (1, 1)
        for name in "sum avg min max bool_and bool_or every array_agg json_agg jsonb_agg stddev stddev_pop stddev_samp variance var_pop var_samp abs ceil ceiling floor sqrt cbrt sign exp ln lower upper initcap length char_length character_length octet_length reverse isfinite jsonb_typeof json_typeof jsonb_array_length json_array_length array_ndims cardinality".split()
    },
    **{
        name: (2, 2)
        for name in "string_agg json_object_agg jsonb_object_agg mod power left right strpos position date_part extract timezone to_char to_date array_length".split()
    },
    **{name: (0, 0) for name in "row_number rank dense_rank percent_rank cume_dist".split()},
    **{name: (1, 2) for name in "round trunc log btrim ltrim rtrim trim age".split()},
    **{
        name: (2, 3)
        for name in "substring substr regexp_match regexp_matches array_position array_positions date_trunc array_to_string".split()
    },
    "count": (0, 1),
    "ntile": (1, 1),
    "lag": (1, 3),
    "lead": (1, 3),
    "first_value": (1, 1),
    "last_value": (1, 1),
    "nth_value": (2, 2),
    "replace": (3, 3),
    "translate": (3, 3),
    "split_part": (3, 3),
    "concat": (1, 1000),
    "concat_ws": (2, 1000),
    "regexp_replace": (3, 6),
    "make_date": (3, 3),
    "make_timestamp": (6, 6),
    "make_timestamptz": (6, 7),
    "to_timestamp": (1, 2),
    "starts_with": (2, 2),
    "jsonb_extract_path": (2, 1000),
    "jsonb_extract_path_text": (2, 1000),
    "percentile_cont": (1, 1),
    "percentile_disc": (1, 1),
    "mode": (0, 0),
    "grouping": (1, 31),
}
WINDOW_ONLY = frozenset(
    "row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value".split()
)
OPERATORS = frozenset(
    "+ - * / % ^ = <> != < > <= >= ~~ !~~ ~~* !~~* || -> ->> #> #>> @> <@ ? ?| ?& && & | # << >> ~ !~ ~* !~*".split()
)
NODES = frozenset(
    """
SelectStmt ResTarget RangeVar Alias JoinExpr RangeSubselect WithClause CommonTableExpr
ColumnRef A_Const Integer Float String Boolean BitString A_Expr BoolExpr NullTest BooleanTest
FuncCall NamedArgExpr TypeCast TypeName ParamRef CaseExpr CaseWhen CoalesceExpr MinMaxExpr
SubLink RowExpr A_ArrayExpr A_Indirection A_Indices SortBy WindowDef GroupingSet GroupingFunc
SQLValueFunction SetToDefault
""".split()
)


def _strings(values: list) -> list[str]:
    return [x["String"]["sval"] for x in values]


def _type_name(node: dict) -> str:
    names = _strings(node.get("names", []))
    if len(names) > 2 or (len(names) == 2 and names[0] != "pg_catalog"):
        raise SqlError(
            "UNSUPPORTED_SQL_FEATURE", "Only approved pg_catalog cast types are supported."
        )
    value = normalize_type(names[-1] if names else "")
    if value not in TYPES:
        raise SqlError("UNSUPPORTED_SQL_FEATURE", "Cast type is not in the supported type policy.")
    if node.get("arrayBounds"):
        value += "[]"
    return value


def _native(sql: str, catalog: Catalog) -> tuple[dict, dict[int, str], set[str]]:
    if len(sql.encode()) > MAX_SQL_BYTES:
        raise SqlError("INPUT_TOO_LARGE", "SQL exceeds the 256 KiB limit.", "sql")
    try:
        tree = json.loads(parser.parse_sql_json(sql))
    except (parser.ParseError, ValueError, RecursionError):
        raise SqlError(
            "SQL_PARSE_ERROR", "PostgreSQL could not parse the statement.", "sql"
        ) from None
    if len(tree.get("stmts", [])) != 1 or "SelectStmt" not in tree["stmts"][0]["stmt"]:
        raise SqlError(
            "READ_ONLY_VIOLATION", "Exactly one analytical SELECT statement is required.", "sql"
        )
    stack = [(tree["stmts"][0]["stmt"], 0, None)]
    count, params, casts, functions, native_relations = 0, set(), {}, set(), set()
    while stack:
        node, depth, parent = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise SqlError("INPUT_TOO_LARGE", "SQL exceeds the AST node or nesting budget.", "sql")
        if isinstance(node, dict):
            for kind, value in node.items():
                if kind and kind[0].isupper():
                    if kind in {
                        "InsertStmt",
                        "UpdateStmt",
                        "DeleteStmt",
                        "MergeStmt",
                        "IntoClause",
                        "LockingClause",
                    }:
                        raise SqlError(
                            "READ_ONLY_VIOLATION", "Nested writes and locking are forbidden.", "sql"
                        )
                    if kind not in NODES:
                        raise SqlError(
                            "UNSUPPORTED_SQL_FEATURE",
                            f"SQL construct {kind} is outside the supported policy.",
                            "sql",
                        )
                    if kind == "JoinExpr" and (value.get("isNatural") or value.get("usingClause")):
                        raise SqlError(
                            "UNSUPPORTED_SQL_FEATURE",
                            "Use explicit JOIN ON predicates instead of NATURAL/USING.",
                            "sql",
                        )
                    if kind == "SelectStmt" and (
                        value.get("intoClause") or value.get("lockingClause")
                    ):
                        raise SqlError(
                            "READ_ONLY_VIOLATION",
                            "SELECT INTO and locking clauses are forbidden.",
                            "sql",
                        )
                    if kind == "SetToDefault":
                        raise SqlError(
                            "UNSUPPORTED_SQL_FEATURE", "DEFAULT expressions are unsupported.", "sql"
                        )
                    if kind == "ColumnRef" and any("A_Star" in field for field in value["fields"]):
                        raise SqlError(
                            "UNSUPPORTED_SQL_FEATURE",
                            "Select explicit columns; projection wildcards are forbidden.",
                            "sql",
                        )
                    if kind == "RangeVar":
                        schema, name = value.get("schemaname"), value["relname"]
                        if value.get("catalogname"):
                            raise SqlError(
                                "OBJECT_NOT_AUTHORIZED",
                                "Cross-database identifiers are forbidden.",
                                "sql",
                            )
                        if schema:
                            key = f"{schema}.{name}"
                            relation = catalog.relation_map.get(key)
                            if (
                                not relation
                                or schema.startswith("pg_")
                                or schema == "information_schema"
                            ):
                                raise SqlError(
                                    "OBJECT_NOT_AUTHORIZED",
                                    "A referenced object is unavailable for this identity.",
                                    "sql",
                                )
                            if value.get("inh") and any(
                                d not in catalog.relation_map for d in relation.descendants
                            ):
                                raise SqlError(
                                    "OBJECT_NOT_AUTHORIZED",
                                    "Inherited reads include unauthorized dependencies; use ONLY where appropriate.",
                                    "sql",
                                )
                            native_relations.add(key)
                    if kind == "FuncCall":
                        name = _strings(value["funcname"])
                        if (
                            len(name) > 2
                            or (len(name) == 2 and name[0] != "pg_catalog")
                            or name[-1] not in FUNCTIONS
                        ):
                            raise SqlError(
                                "READ_ONLY_VIOLATION",
                                "Function is not approved by the built-in function policy.",
                                "sql",
                            )
                        count_args = len(value.get("args", []))
                        low, high = ARITIES.get(name[-1], (1, 1000))
                        if (
                            not low <= count_args <= high
                            or (
                                name[-1] == "count"
                                and count_args == 0
                                and not value.get("agg_star")
                            )
                            or (name[-1] in WINDOW_ONLY and not value.get("over"))
                        ):
                            raise SqlError(
                                "SQL_TYPE_ERROR",
                                "Function argument count or window usage is invalid.",
                                "sql",
                            )
                        functions.add(name[-1])
                    if kind == "A_Expr":
                        op = _strings(value.get("name", []))
                        if op and (
                            op[-1] not in OPERATORS or (len(op) > 1 and op[:-1] != ["pg_catalog"])
                        ):
                            raise SqlError(
                                "UNSUPPORTED_SQL_FEATURE", "Operator is not approved.", "sql"
                            )
                    if kind == "TypeName":
                        _type_name(value)
                    if kind == "ParamRef":
                        position = value["number"]
                        params.add(position)
                        if (
                            not parent
                            or "TypeCast" not in parent
                            or parent["TypeCast"].get("arg") != node
                        ):
                            raise SqlError(
                                "PARAMETER_MISMATCH",
                                "Every parameter occurrence requires an explicit PostgreSQL cast.",
                                "sql",
                            )
                    if kind == "TypeCast" and "ParamRef" in value.get("arg", {}):
                        position = value["arg"]["ParamRef"]["number"]
                        datatype = _type_name(value["typeName"])
                        if position in casts and casts[position] != datatype:
                            raise SqlError(
                                "PARAMETER_MISMATCH", "Repeated parameter casts disagree.", "sql"
                            )
                        casts[position] = datatype
                stack.append((value, depth + 1, node if kind == "TypeCast" else parent))
        elif isinstance(node, list):
            stack.extend((v, depth + 1, parent) for v in node)
    if params != set(range(1, len(params) + 1)):
        raise SqlError(
            "PARAMETER_MISMATCH", "Parameter positions must be contiguous from 1.", "parameters"
        )
    return native_relations, casts, functions


def _pg_aggregate(annotator: TypeAnnotator, node: exp.Expr) -> exp.Expr:
    source = node.this
    if isinstance(source, exp.Distinct):
        source = source.expressions[0]
    value = (
        normalize_type(source.type.sql(dialect="postgres"))
        if source is not None and source.type
        else "unknown"
    )
    if value not in {
        "smallint",
        "integer",
        "bigint",
        "numeric",
        "real",
        "double precision",
        "interval",
    }:
        raise SqlError(
            "SQL_TYPE_ERROR", "SUM/AVG require a supported numeric or interval input.", "sql"
        )
    if isinstance(node, exp.Sum):
        result = {"smallint": "bigint", "integer": "bigint", "bigint": "numeric"}.get(value, value)
    else:
        result = (
            "numeric"
            if value in {"smallint", "integer", "bigint", "numeric"}
            else ("double precision" if value == "real" else value)
        )
    if result not in TYPES:
        raise SqlError(
            "UNSUPPORTED_SQL_FEATURE", "Aggregate argument type could not be resolved.", "sql"
        )
    return annotator._set_type(node, exp.DataType.build(result, dialect="postgres"))


class PgTypeAnnotator(TypeAnnotator):
    def _get_scope_source_selects(self, scope: Scope, source_name: str):
        source = scope.sources.get(source_name)
        if isinstance(source, Scope) and isinstance(source.expression, exp.Lateral):
            query = source.expression.this.unnest()
            return {p.alias_or_name: p.type for p in query.selects}
        return super()._get_scope_source_selects(scope, source_name)


def _pg_metadata() -> dict:
    text_functions = [
        exp.Upper,
        exp.Lower,
        exp.Initcap,
        exp.Substring,
        exp.Replace,
        exp.Concat,
        exp.ConcatWs,
        exp.DPipe,
        exp.Trim,
        exp.Left,
        exp.Right,
        exp.Reverse,
        exp.SplitPart,
        exp.RegexpReplace,
        exp.ArrayToString,
        exp.TimeToStr,
    ]
    values = {cls: {"returns": exp.DType.TEXT} for cls in text_functions}
    values[exp.ArraySize] = {"returns": exp.DType.INT}
    values[exp.Round] = {"annotator": lambda a, n: a._set_type(n, n.this.type)}

    def add_sub(a, n):
        left = normalize_type(n.this.type.sql(dialect="postgres")) if n.this.type else "unknown"
        right = (
            normalize_type(n.expression.type.sql(dialect="postgres"))
            if n.expression.type
            else "unknown"
        )
        if {left, right} == {"date", "interval"}:
            result = "timestamp without time zone"
        elif left == "date" and right == "date" and isinstance(n, exp.Sub):
            result = "integer"
        else:
            return a._annotate_binary(n)
        return a._set_type(n, exp.DataType.build(result, dialect="postgres"))

    values[exp.Add] = {"annotator": add_sub}
    values[exp.Sub] = {"annotator": add_sub}

    def timezone_type(a, n):
        value = normalize_type(n.this.type.sql(dialect="postgres")) if n.this.type else "unknown"
        target = {
            "timestamp with time zone": "timestamp without time zone",
            "timestamp without time zone": "timestamp with time zone",
        }.get(value)
        return a._set_type(
            n, exp.DataType.build(target, dialect="postgres") if target else exp.DType.UNKNOWN
        )

    values[exp.AtTimeZone] = {"annotator": timezone_type}
    values[exp.JSONExtractScalar] = {"returns": exp.DType.TEXT}
    values[exp.JSONExtract] = {"annotator": lambda a, n: a._set_type(n, n.this.type)}
    return values


class PostgresAnalyzer:
    def analyze(self, sql: str, catalog: Catalog) -> dict:
        if catalog.dialect != "postgresql" or catalog.postgres_major != 18:
            raise SqlError("UNSUPPORTED_DATABASE_VERSION", "Only PostgreSQL 18 is tested.")
        native_relations, params, functions = _native(sql, catalog)
        schema_data = {}
        for relation in catalog.relations:
            cols = {}
            for col in relation.columns:
                typename = normalize_type(col.postgres_type)
                if col.type_schema != "pg_catalog" or typename.removesuffix("[]") not in TYPES:
                    # Omit unsupported custom types from resolver, but never silently
                    # omit a requested reference: qualification will reject it.
                    continue
                cols[col.name] = col.postgres_type
            schema_data.setdefault(relation.schema, {})[relation.name] = cols
        schema = MappingSchema(schema_data, dialect="postgres", normalize=False)
        try:
            parsed = sqlglot.parse(sql, read="postgres", error_level=ErrorLevel.RAISE)
            if len(parsed) != 1 or not isinstance(parsed[0], (exp.Select, exp.SetOperation)):
                raise SqlError(
                    "UNSUPPORTED_SQL_FEATURE", "Resolver does not support this statement.", "sql"
                )
            query = normalize_identifiers(parsed[0], dialect="postgres")
            # Resolve base relations BEFORE qualification can infer/repair names.
            for scope in traverse_scope(query):
                for _, (_, source) in scope.selected_sources.items():
                    if isinstance(source, exp.Table):
                        relation = catalog.relation_map.get(f"{source.db}.{source.name}")
                        if not source.db or source.catalog or not relation:
                            raise SqlError(
                                "OBJECT_NOT_AUTHORIZED",
                                "Base tables require an authorized explicit schema.",
                                "sql",
                            )
                        if source.alias_column_names:
                            raise SqlError(
                                "UNSUPPORTED_SQL_FEATURE",
                                "Base-table column alias lists are unsupported.",
                                "sql",
                            )
            query = qualify(
                query,
                dialect="postgres",
                schema=schema,
                expand_stars=False,
                validate_qualify_columns=True,
            )
            scopes = list(traverse_scope(query))
            dependencies = set()
            for scope in scopes:
                for col in scope.columns:
                    search = scope
                    while search and col.table not in search.sources:
                        search = search.parent
                    source = search.sources.get(col.table) if search else None
                    if isinstance(source, exp.Table):
                        relation = catalog.relation_map.get(f"{source.db}.{source.name}")
                        if (
                            not relation
                            or col.name not in relation.column_map
                            or col.name not in schema_data[relation.schema][relation.name]
                        ):
                            raise SqlError(
                                "OBJECT_NOT_AUTHORIZED",
                                "A column is unavailable or its type is unsupported.",
                                "sql",
                            )
                        dependencies.add(f"{relation.key}.{col.name}")
                        for child in () if source.args.get("only") else relation.descendants:
                            descendant = catalog.relation_map.get(child)
                            if not descendant or col.name not in descendant.column_map:
                                raise SqlError(
                                    "OBJECT_NOT_AUTHORIZED",
                                    "Inherited column dependency is unavailable.",
                                    "sql",
                                )
                            dependencies.add(f"{child}.{col.name}")
            warnings = check_scopes(scopes, catalog)
            parameter_predicates, parameter_expressions, parameter_ranges = {}, {}, {}
            for parameter in query.find_all(exp.Parameter):
                position = int(parameter.name)
                predicate = parameter.find_ancestor(exp.Where, exp.Having, exp.Join)
                if not predicate:
                    raise SqlError(
                        "PARAMETER_MISMATCH",
                        "Runtime filters must affect a WHERE, HAVING or JOIN predicate.",
                        "parameters",
                    )
                cast = parameter.parent
                comparison = cast.parent if isinstance(cast, exp.Cast) else None
                operator, compared_expression = None, None
                if isinstance(comparison, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)):
                    operator = comparison.key
                    compared_expression = (
                        comparison.expression if comparison.this is cast else comparison.this
                    )
                    if comparison.this is cast:
                        operator = {"lt": "gt", "lte": "gte", "gt": "lt", "gte": "lte"}.get(
                            operator, operator
                        )
                path = comparison.parent if comparison is not None else None
                conjunctive = True
                while path is not None and path is not predicate:
                    if not isinstance(path, (exp.And, exp.Paren)):
                        conjunctive = False
                    path = path.parent
                parameter_ranges.setdefault(position, []).append(
                    {"clause": id(predicate), "conjunctive": conjunctive and path is predicate}
                )
                parameter_predicates.setdefault(position, []).append(operator)
                comparable = compared_expression is not None and (
                    compared_expression.find(exp.Column) is not None
                    or compared_expression.find(exp.AggFunc) is not None
                )
                parameter_expressions.setdefault(position, []).append(
                    compared_expression.sql() if comparable else None
                )

            metadata = dict(Dialect.get_or_raise("postgres").EXPRESSION_METADATA)
            metadata[exp.Sum] = {"annotator": _pg_aggregate}
            metadata[exp.Avg] = {"annotator": _pg_aggregate}
            metadata.update(_pg_metadata())
            PgTypeAnnotator(schema, expression_metadata=metadata).annotate(query)
            self._check_types(query)
            type_policy.check(query, normalize_type)
            names = [p.alias_or_name for p in query.selects]
            if len(names) != len(set(names)) or any(not n for n in names):
                raise SqlError(
                    "OUTPUT_CONTRACT_MISMATCH",
                    "Output keys must be explicit, unique names.",
                    "outputs",
                )
            outputs = []
            graph = lineage(None, query, schema, dialect="postgres", copy=True)
            for projection in query.selects:
                key = projection.alias_or_name
                datatype = (
                    normalize_type(projection.type.sql(dialect="postgres"))
                    if projection.type
                    else "unknown"
                )
                if datatype.removesuffix("[]") not in TYPES:
                    raise SqlError(
                        "UNSUPPORTED_SQL_FEATURE",
                        "Output type could not be resolved; use an explicit supported cast.",
                        "outputs",
                    )
                origins = set()
                for node in graph[key].walk():
                    if isinstance(node.expression, exp.Table):
                        # The lineage node name contains alias.column, quoted as needed.
                        leaf = sqlglot.parse_one(node.name, read="postgres")
                        if isinstance(leaf, exp.Column):
                            origins.add(f"{node.expression.db}.{node.expression.name}.{leaf.name}")
                outputs.append(
                    {
                        "key": key,
                        "postgresType": datatype,
                        "logicalType": logical_type(datatype),
                        "nullable": None,
                        "lineage": sorted(origins),
                    }
                )
            relations = sorted(
                {
                    f"{s.db}.{s.name}"
                    for scope in scopes
                    for _, (_, s) in scope.selected_sources.items()
                    if isinstance(s, exp.Table)
                }
            )
            if set(relations) != native_relations:
                raise SqlError(
                    "UNSUPPORTED_SQL_FEATURE",
                    "The syntax parser and resolver disagree on relation dependencies.",
                    "sql",
                )
            relations = sorted(
                set(relations)
                | {
                    child
                    for scope in scopes
                    for _, (_, source) in scope.selected_sources.items()
                    if isinstance(source, exp.Table) and not source.args.get("only")
                    for child in catalog.relation_map[f"{source.db}.{source.name}"].descendants
                }
            )
            if (
                sum(
                    isinstance(source, exp.Table)
                    for scope in scopes
                    for _, (_, source) in scope.selected_sources.items()
                )
                > 1000
            ):
                raise SqlError("INPUT_TOO_LARGE", "Relation reference limit exceeded.", "sql")
            return {
                "parameters": params,
                "parameterPredicates": parameter_predicates,
                "parameterExpressions": parameter_expressions,
                "parameterRanges": parameter_ranges,
                "outputs": outputs,
                "dependencies": {
                    "relations": relations,
                    "columns": sorted(dependencies),
                    "functions": [f"pg_catalog.{f}" for f in sorted(functions)],
                },
                "warnings": warnings,
            }
        except SqlError:
            raise
        except (SqlglotError, ValueError, TypeError, KeyError, RecursionError, AttributeError):
            raise SqlError(
                "UNSUPPORTED_SQL_FEATURE",
                "Catalog resolution or type analysis could not establish this query; simplify or use a supported explicit cast.",
                "sql",
            ) from None

    @staticmethod
    def _check_types(query: exp.Expr) -> None:
        numeric = {"smallint", "integer", "bigint", "numeric", "real", "double precision"}
        for node in query.walk():
            if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
                left = (
                    normalize_type(node.this.type.sql(dialect="postgres"))
                    if node.this.type
                    else "unknown"
                )
                right = (
                    normalize_type(node.expression.type.sql(dialect="postgres"))
                    if node.expression.type
                    else "unknown"
                )
                if left in numeric and right in numeric:
                    continue
                if isinstance(node, (exp.Add, exp.Sub)) and (
                    {left, right}
                    & {
                        "date",
                        "timestamp without time zone",
                        "timestamp with time zone",
                        "interval",
                    }
                ):
                    continue
                raise SqlError(
                    "SQL_TYPE_ERROR",
                    "Arithmetic operands have unsupported or incompatible types.",
                    "sql",
                )
            if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                left, right = node.this.type, node.expression.type
                if left and right:
                    a, b = (
                        normalize_type(left.sql(dialect="postgres")),
                        normalize_type(right.sql(dialect="postgres")),
                    )
                    if isinstance(node.expression, (exp.Any, exp.All)):
                        b = normalize_type(
                            node.expression.this.type.sql(dialect="postgres")
                        ).removesuffix("[]")
                    if (
                        a != b
                        and not ({a, b} <= numeric)
                        and not any(
                            isinstance(x, (exp.Literal, exp.Null))
                            for x in (node.this, node.expression)
                        )
                    ):
                        raise SqlError(
                            "SQL_TYPE_ERROR",
                            "Comparison operands have incompatible types; cast explicitly.",
                            "sql",
                        )
