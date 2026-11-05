"""Published dialect capabilities and binding policies; no request or session state."""

from atlas_sql_mcp.analysis.limits import MAX_DEPTH, MAX_NODES, MAX_SQL_BYTES
from atlas_sql_mcp.analysis.postgres import FUNCTIONS
from atlas_sql_mcp.domain import Catalog, SqlError

INPUT_BYTES = 512 * 1024
OUTPUT_BYTES = 1024 * 1024
CONTEXT_SECONDS = 600
REQUEST_SECONDS = 20


def validate_contract_version(args: dict, catalog: Catalog) -> None:
    expected = "1.1" if catalog.dialect == "sqlserver" else "1.0"
    if args["contractVersion"] not in {expected, "1.2"}:
        raise SqlError(
            "INVALID_CONTRACT",
            f"This connection requires contractVersion {expected}.",
            "contractVersion",
        )


def capabilities(dialect="postgresql") -> dict:
    if dialect == "sqlserver":
        from atlas_sql_mcp.analysis.sqlserver import FUNCTIONS as TSQL_FUNCTIONS
        from atlas_sql_mcp.analysis.sqlserver import SESSION_SETTINGS

        base = capabilities()
        base.pop("postgresMajorVersions")
        base["databaseMajorVersions"] = [16, 17]
        base["requiredSessionSettings"] = dict(SESSION_SETTINGS)
        base["features"] = [
            "qualified_base_tables",
            "governed_joins",
            "nonrecursive_ctes",
            "derived_tables",
            "numeric_aggregates",
            "conditional_aggregates",
            "ranking_windows",
            "typed_named_parameters",
            "ordered_top",
        ]
        base["unsupported"] += [
            "native_server_syntax_verification",
            "set_operations",
            "recursive_ctes",
            "aggregate_windows",
            "implicit_type_coercion",
            "decimal_scale_reduction",
            "query_hints",
            "temporal_tables",
            "computed_or_masked_columns",
        ]
        base["approvedFunctions"] = sorted(TSQL_FUNCTIONS | {"cast"})
        return base
    return {
        "postgresMajorVersions": [18],
        "features": [
            "qualified_base_tables",
            "governed_joins",
            "ctes",
            "recursive_ctes",
            "correlated_subqueries",
            "window_functions",
            "calculations",
            "grouping_sets",
            "set_operations",
            "typed_positional_parameters",
            "large_queries",
        ],
        "unsupported": [
            "views_without_dependency_attestation",
            "custom_types_or_functions",
            "projection_wildcards",
            "natural_or_using_joins",
            "cartesian_joins",
            "table_valued_functions",
            "nullable_runtime_parameters",
            "query_execution",
            "cost_estimation",
        ],
        "approvedFunctions": sorted(FUNCTIONS),
        "requiredSessionSettings": {"search_path": "pg_catalog"},
        "limits": {
            "sqlBytes": MAX_SQL_BYTES,
            "inputBytes": INPUT_BYTES,
            "outputBytes": OUTPUT_BYTES,
            "astNodes": MAX_NODES,
            "astDepth": MAX_DEPTH,
            "contextSeconds": CONTEXT_SECONDS,
            "analysisSeconds": 7,
        },
        "businessRowsRead": False,
        "queryExecutionAvailable": False,
        "parameterCastsRequired": True,
    }
