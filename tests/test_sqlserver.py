"""Static T-SQL conformance and negative tests, independent of a destination."""

from copy import deepcopy
from dataclasses import replace

import pytest
from conftest import MemoryCatalog, args_for, with_range

from atlas_sql_mcp.analysis.sqlserver import SqlServerAnalyzer, check_default, normalize_type
from atlas_sql_mcp.analysis.worker import ProcessAnalyzer
from atlas_sql_mcp.application.service import SqlService
from atlas_sql_mcp.contracts import SCHEMAS, validate
from atlas_sql_mcp.domain import Catalog, Column, Constraint, Principal, Relation, SqlError


@pytest.fixture
def tsql_catalog():
    return Catalog(
        "sales",
        16,
        (
            Relation(
                "dbo",
                "orders",
                (
                    Column("id", "int", False, type_schema="sys"),
                    Column("customer_id", "int", False, type_schema="sys"),
                    Column("ordered_on", "date", False, type_schema="sys"),
                    Column("amount", "decimal(20,2)", False, type_schema="sys"),
                    Column("quantity", "smallint", False, type_schema="sys"),
                    Column("name", "nvarchar(80)", True, type_schema="sys"),
                    Column("active", "bit", False, type_schema="sys"),
                ),
                (Constraint("orders_pk", "p", ("id",)),),
            ),
            Relation(
                "dbo",
                "customers",
                (
                    Column("id", "int", False, type_schema="sys"),
                    Column("name", "nvarchar(80)", False, type_schema="sys"),
                ),
                (Constraint("customers_pk", "p", ("id",)),),
            ),
        ),
        "fixture-policy",
        "sqlserver",
    )


@pytest.mark.parametrize(
    "sql,types",
    [
        ("SELECT id FROM dbo.orders", ["int"]),
        (
            "SELECT SUM(CASE WHEN quantity>0 THEN amount ELSE 0 END) AS total FROM dbo.orders",
            ["decimal(38,2)"],
        ),
        ("SELECT COALESCE(quantity, 0) AS quantity FROM dbo.orders", ["int"]),
        ("SELECT CASE quantity WHEN 1 THEN id ELSE 0 END AS result FROM dbo.orders", ["int"]),
        ("SELECT SUM(quantity) AS total, AVG(quantity) AS average FROM dbo.orders", ["int", "int"]),
        (
            "SELECT SUM(amount) AS total, AVG(amount) AS average FROM dbo.orders",
            ["decimal(38,2)", "decimal(38,6)"],
        ),
        ("SELECT COUNT(*) AS n, COUNT_BIG(*) AS big FROM dbo.orders", ["int", "bigint"]),
        (
            "SELECT MIN(ordered_on) AS first_day, MAX(name) AS last_name FROM dbo.orders",
            ["date", "nvarchar(80)"],
        ),
        ("SELECT id + quantity AS n FROM dbo.orders", ["int"]),
        (
            "SELECT CAST(id AS decimal(9,0)) * CAST(quantity AS decimal(5,0)) AS n FROM dbo.orders",
            ["decimal(15,0)"],
        ),
        ("SELECT id / NULLIF(quantity,0) AS n FROM dbo.orders", ["int"]),
        ("SELECT ROW_NUMBER() OVER(ORDER BY id) AS n FROM dbo.orders", ["bigint"]),
        ("SELECT TOP(5) id FROM dbo.orders ORDER BY id", ["int"]),
        ("WITH a AS (SELECT id FROM dbo.orders) SELECT id FROM a", ["int"]),
        ("SELECT a.id FROM (SELECT id FROM dbo.orders) a", ["int"]),
        (
            "SELECT c.name, SUM(o.amount) AS total FROM dbo.orders o JOIN dbo.customers c ON c.id=o.customer_id GROUP BY c.name",
            ["nvarchar(80)", "decimal(38,2)"],
        ),
        ("SELECT [o].[id] AS [Order ID] FROM [dbo].[orders] [o]", ["int"]),
    ],
)
def test_supported_tsql(tsql_catalog, sql, types):
    result = SqlServerAnalyzer().analyze(sql, tsql_catalog)
    assert [o["sqlType"] for o in result["outputs"]] == types
    assert result["dependencies"]["relations"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM dbo.orders; SELECT id FROM dbo.orders",
        "SELECT id INTO dbo.copy FROM dbo.orders",
        "DELETE FROM dbo.orders",
        "EXEC sys.sp_who",
        "SELECT * FROM dbo.orders",
        "SELECT id FROM otherdb.dbo.orders",
        "SELECT id FROM orders",
        "SELECT secret FROM dbo.orders",
        "SELECT id FROM dbo.orders WITH(NOLOCK)",
        "SELECT id FROM dbo.orders OPTION(RECOMPILE)",
        "SELECT dbo.unknown(id) AS x FROM dbo.orders",
        "SELECT NEWID() AS x FROM dbo.orders",
        "SELECT SUM(name) AS x FROM dbo.orders",
        "SELECT AVG(active) AS x FROM dbo.orders",
        "SELECT id, SUM(amount) AS total FROM dbo.orders",
        "SELECT id, name, SUM(amount) AS total FROM dbo.orders GROUP BY id",
        "SELECT ROW_NUMBER() OVER() AS n FROM dbo.orders",
        "SELECT TOP(5) id FROM dbo.orders",
        "SELECT TOP(5) PERCENT id FROM dbo.orders ORDER BY id",
        "SELECT id FROM dbo.orders LIMIT 5",
        "SELECT id::int AS n FROM dbo.orders",
        "SELECT id FROM dbo.orders WHERE id=@@SPID",
        "SELECT id FROM dbo.orders WHERE id=@p1",
        "SELECT id FROM dbo.orders WHERE id=CAST(@p2 AS int)",
        "SELECT CAST(@p1 AS int) AS x FROM dbo.orders",
        "SELECT id FROM dbo.orders WHERE id=CAST(@p1 AS int) OR id=CAST(@p1 AS bigint)",
        "SELECT id FROM dbo.orders UNION SELECT id FROM dbo.orders",
        "SELECT id FROM dbo.orders CROSS JOIN dbo.customers",
        "SELECT SUM(c.id) AS n FROM dbo.customers c JOIN dbo.orders o ON c.id=o.customer_id",
        "SELECT id / quantity AS n FROM dbo.orders",
        "SELECT amount * amount AS n FROM dbo.orders",
        "SELECT CAST(id AS geography) AS x FROM dbo.orders",
        "SELECT id FROM dbo.orders WHERE ordered_on=CAST(@p1 AS int)",
        "SELECT id FROM dbo.orders WHERE id",
        "SELECT id FROM dbo.orders WHERE CAST(id AS bit)",
        "SELECT id FROM dbo.orders WHERE ROW_NUMBER() OVER(ORDER BY id)=1",
        "SELECT SUM(amount) AS total FROM dbo.orders HAVING id=1",
        "SELECT SUM(amount) AS total FROM dbo.orders ORDER BY id",
        "SELECT CAST(id AS date) AS day FROM dbo.orders",
        "SELECT id FROM dbo.orders WHERE ordered_on BETWEEN 1 AND 2",
        "SELECT id FROM dbo.orders WHERE ordered_on IN (1,2)",
    ],
)
def test_unsupported_and_unsafe_queries_fail(tsql_catalog, sql):
    with pytest.raises(SqlError):
        SqlServerAnalyzer().analyze(sql, tsql_catalog)


def test_authorization_in_nested_ctes(tsql_catalog):
    sql = "WITH a AS (SELECT id FROM private.orders) SELECT id FROM a"
    with pytest.raises(SqlError):
        SqlServerAnalyzer().analyze(sql, tsql_catalog)


def test_full_service_and_isolated_analyzer(tsql_catalog):
    principal = Principal("local", "analyst")
    service = SqlService(MemoryCatalog(tsql_catalog), ProcessAnalyzer())
    ctx = service.call(
        "atlas_sql_context_get",
        {"contractVersion": "1.1", "connectionId": "sales", "request": "orders in period"},
        principal,
    )
    assert ctx["status"] == "context", ctx
    assert ctx["dialect"] == "sqlserver"
    assert "postgresMajorVersion" not in ctx
    assert "postgresType" not in ctx["relations"][0]["columns"][0]
    args = with_range(
        args_for(
            "SELECT id FROM dbo.orders WHERE ordered_on>=CAST(@p1 AS date) AND ordered_on<CAST(@p2 AS date)",
            ["id"],
            ctx["contextId"],
        )
    )
    args["contractVersion"] = "1.1"
    for parameter in args["parameters"]:
        parameter["sqlType"] = parameter.pop("postgresType")
    result = service.call("atlas_sql_finalize", args, principal)
    assert result["status"] == "validated", result
    artifact = result["artifact"]
    assert artifact["sql"] == args["sql"]
    assert artifact["outputs"][0]["lineage"] == ["dbo.orders.id"]
    assert artifact["validation"]["syntax"] == "passed_static_subset"
    assert artifact["validation"]["executionPerformed"] is False
    assert artifact["requiredSessionSettings"]["QUOTED_IDENTIFIER"] == "ON"
    validate(SCHEMAS["atlas_sql_finalize"][1], result)
    repeat = service.call("atlas_sql_finalize", args, principal)
    assert artifact["artifactHash"] == repeat["artifact"]["artifactHash"]
    for mutate in [
        lambda a: a.update(contractVersion="1.0"),
        lambda a: a["parameters"][0].update(sqlType="int"),
        lambda a: a.update(sql=a["sql"].replace(">=", ">")),
        lambda a: a["outputs"][0].update(sqlType="bigint"),
    ]:
        changed = deepcopy(args)
        mutate(changed)
        assert service.call("atlas_sql_finalize", changed, principal)["status"] == "error"
    service.catalog.value = replace(tsql_catalog, policy_version="revoked")
    assert service.call("atlas_sql_finalize", args, principal)["error"]["code"] == "CONTEXT_STALE"


@pytest.mark.parametrize(
    "datatype,value",
    [
        ("tinyint", "256"),
        ("tinyint", "-1"),
        ("decimal(4,2)", "100"),
        ("decimal(4,2)", "1.234"),
        ("nvarchar(2)", "abc"),
        ("nvarchar(2)", "😀x"),
        ("varchar(5)", "é"),
        ("datetimeoffset(7)", "2026-01-01T00:00:00"),
        ("datetime2(7)", "2026-01-01T00:00:00Z"),
    ],
)
def test_lossless_tsql_defaults(datatype, value):
    with pytest.raises(SqlError):
        check_default({"type": "text", "nullable": False, "default": value}, datatype)


@pytest.mark.parametrize(
    "datatype",
    [
        "nchar(max)",
        "decimal(39,2)",
        "decimal(5,6)",
        "varchar(8001)",
        "datetime2(8)",
        "xml",
        "hierarchyid",
        "sys.int",
    ],
)
def test_unknown_types_fail(datatype):
    with pytest.raises(SqlError):
        normalize_type(datatype)


def test_sqlserver_fixtures_are_executable_contracts(tsql_catalog):
    import json
    from pathlib import Path

    from atlas_sql_mcp.config import load_connections

    root = Path(__file__).resolve().parents[1]
    assert (
        load_connections(root / "tests/fixtures/connections.json")["sales-sqlserver"].dialect
        == "sqlserver"
    )
    request = json.loads((root / "tests/fixtures/payloads/finalize-sqlserver.json").read_text())
    validate(SCHEMAS["atlas_sql_finalize"][0], request)
    service = SqlService(MemoryCatalog(tsql_catalog), SqlServerAnalyzer())
    principal = Principal("local", "analyst")
    ctx = service.call(
        "atlas_sql_context_get",
        {"contractVersion": "1.1", "connectionId": "sales", "request": "orders in period"},
        principal,
    )
    request["contextId"] = ctx["contextId"]
    assert service.call("atlas_sql_finalize", request, principal)["status"] == "validated"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM dbo.orders WHERE id >",
        "SELECT id FROM dbo.orders WHERE (id=1",
        "SELECT SUM(amount) OVER(ORDER BY id) AS n FROM dbo.orders",
        "SELECT id, ROW_NUMBER() OVER(ORDER BY SUM(quantity)) AS n FROM dbo.orders",
        "SELECT COUNT(*) AS n FROM dbo.orders GROUP BY name + quantity",
        "SELECT CASE WHEN id THEN amount ELSE 0 END AS n FROM dbo.orders",
        "SELECT COALESCE(name, id) AS n FROM dbo.orders",
    ],
)
def test_tsql_semantic_regressions(tsql_catalog, sql):
    with pytest.raises(SqlError):
        SqlServerAnalyzer().analyze(sql, tsql_catalog)


def test_wrong_context_version_does_not_consume_storage(tsql_catalog):
    service = SqlService(MemoryCatalog(tsql_catalog), SqlServerAnalyzer())
    result = service.call(
        "atlas_sql_context_get",
        {"contractVersion": "1.0", "connectionId": "sales", "request": "orders"},
        Principal("local", "analyst"),
    )
    assert result["error"]["code"] == "INVALID_CONTRACT"
    assert "1.1" in result["error"]["message"]
    assert not service.state.contexts


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM dbo.orders HAVING id>0",
        "SELECT 1 AS n FROM dbo.orders HAVING id>0",
        "SELECT DISTINCT id FROM dbo.orders ORDER BY quantity",
        "SELECT COUNT(DISTINCT c.id) AS n FROM dbo.orders o JOIN dbo.customers c "
        "ON c.id=o.customer_id AND COUNT(DISTINCT c.id)>0",
    ],
)
def test_reject_invalid_tsql_clause_semantics(tsql_catalog, sql):
    with pytest.raises(SqlError):
        SqlServerAnalyzer().analyze(sql, tsql_catalog)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DISTINCT id AS order_id FROM dbo.orders ORDER BY order_id",
        "SELECT COUNT(*) AS n FROM dbo.orders HAVING COUNT(*)>0",
        "SELECT id FROM dbo.orders WHERE EXISTS "
        "(SELECT COUNT(*) AS n FROM dbo.customers HAVING COUNT(*)>0)",
    ],
)
def test_valid_tsql_distinct_and_implicit_group(tsql_catalog, sql):
    assert SqlServerAnalyzer().analyze(sql, tsql_catalog)["outputs"]


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("CAST(id AS smallint)+quantity", "smallint"),
        ("CAST(id AS tinyint)+CAST(quantity AS tinyint)", "tinyint"),
        ("-CAST(id AS tinyint)", "smallint"),
        ("CAST(ordered_on AS datetime)", "datetime"),
        ("CAST(ordered_on AS smalldatetime)", "smalldatetime"),
        ("CAST(name AS nvarchar(max))", "nvarchar(max)"),
    ],
)
def test_native_erp_types_and_integer_precedence(tsql_catalog, expression, expected):
    result = SqlServerAnalyzer().analyze(
        f"SELECT {expression} AS result FROM dbo.orders", tsql_catalog
    )
    assert result["outputs"][0]["sqlType"] == expected


@pytest.mark.parametrize(
    "datatype,value",
    [
        ("datetime", "1752-12-31T23:59:59"),
        ("datetime", "2026-01-01T00:00:00.001"),
        ("datetime", "2026-01-01T00:00:00Z"),
        ("smalldatetime", "1899-12-31T23:59:00"),
        ("smalldatetime", "2079-06-07T00:00:00"),
        ("smalldatetime", "2026-01-01T00:00:01"),
    ],
)
def test_legacy_timestamp_defaults_fail_lossy_values(datatype, value):
    with pytest.raises(SqlError):
        check_default({"type": "timestamp", "nullable": False, "default": value}, datatype)


@pytest.mark.parametrize(
    "datatype,value",
    [
        ("datetime", "1753-01-01T00:00:00"),
        ("smalldatetime", "2079-06-06T23:59:00"),
        ("nvarchar(max)", "ERP notes 😀"),
    ],
)
def test_erp_type_defaults_preserve_values(datatype, value):
    check_default({"type": "text", "nullable": False, "default": value}, datatype)


@pytest.fixture
def mixed_case_erp_catalog():
    return Catalog(
        "erp",
        16,
        (
            Relation(
                "dbo",
                "OINV",
                (
                    Column("DocEntry", "int", False, type_schema="sys"),
                    Column("CardCode", "nvarchar(15)", False, type_schema="sys"),
                    Column("DocStatus", "char(1)", False, type_schema="sys"),
                    Column("DocTotal", "decimal(19,6)", False, type_schema="sys"),
                ),
                (Constraint("OINV_PK", "p", ("DocEntry",)),),
            ),
            Relation(
                "dbo",
                "OCRD",
                (
                    Column("CardCode", "nvarchar(15)", False, type_schema="sys"),
                    Column("CardName", "nvarchar(100)", False, type_schema="sys"),
                ),
                (Constraint("OCRD_PK", "p", ("CardCode",)),),
            ),
        ),
        "test",
        "sqlserver",
    )


@pytest.mark.parametrize(
    "sql,keys",
    [
        (
            "SELECT i.DocStatus AS Status, COUNT_BIG(*) AS InvoiceCount, "
            "SUM(i.DocTotal) AS GrossTotal FROM dbo.OINV i "
            "GROUP BY i.DocStatus ORDER BY i.DocStatus",
            ["Status", "InvoiceCount", "GrossTotal"],
        ),
        (
            "SELECT c.CardName AS Customer, SUM(i.DocTotal) AS GrossTotal "
            "FROM dbo.OINV i JOIN dbo.OCRD c ON c.CardCode=i.CardCode "
            "GROUP BY c.CardName ORDER BY GrossTotal",
            ["Customer", "GrossTotal"],
        ),
        (
            "WITH Totals AS (SELECT i.CardCode AS CustomerCode, SUM(i.DocTotal) AS Total "
            "FROM dbo.OINV i GROUP BY i.CardCode) "
            "SELECT t.CustomerCode AS CustomerCode, t.Total AS Revenue "
            "FROM Totals t ORDER BY Revenue",
            ["CustomerCode", "Revenue"],
        ),
        (
            "SELECT [i].[DocEntry] AS [Invoice ID] FROM [dbo].[OINV] [i]",
            ["Invoice ID"],
        ),
    ],
)
def test_mixed_case_catalog_and_aliases_preserve_exact_identifiers(
    mixed_case_erp_catalog, sql, keys
):
    analyzed = SqlServerAnalyzer().analyze(sql, mixed_case_erp_catalog)
    assert [o["key"] for o in analyzed["outputs"]] == keys
    assert "dbo.OINV" in analyzed["dependencies"]["relations"]
    assert any("Doc" in c or "Card" in c for c in analyzed["dependencies"]["columns"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT i.docentry AS id FROM dbo.OINV i",
        "SELECT i.DocEntry AS id FROM dbo.oinv i",
    ],
)
def test_exact_catalog_matching_does_not_assume_case_insensitive_collation(
    mixed_case_erp_catalog, sql
):
    with pytest.raises(SqlError):
        SqlServerAnalyzer().analyze(sql, mixed_case_erp_catalog)
