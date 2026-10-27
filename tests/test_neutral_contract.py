"""Neutral public contracts retain dialect-specific validation and legacy compatibility."""

from copy import deepcopy

import pytest
from conftest import MemoryCatalog, args_for, context_for, with_range
from test_sqlserver import tsql_catalog as sqlserver_catalog_fixture

from atlas_sql_mcp.analyzer import PostgresAnalyzer
from atlas_sql_mcp.contracts import SCHEMAS, validate
from atlas_sql_mcp.domain import SqlError, digest
from atlas_sql_mcp.service import SqlService
from atlas_sql_mcp.sqlserver import SqlServerAnalyzer


@pytest.fixture
def tsql_catalog():
    return sqlserver_catalog_fixture.__wrapped__()


def test_neutral_postgres_bindings_hash_and_sql_integrity(service, principal):
    context = context_for(service, principal, contractVersion="1.2")
    assert context["contractVersion"] == "1.2"
    assert context["databaseMajorVersion"] == 18
    assert context["capabilities"]["databaseMajorVersions"] == [18]
    assert "postgresMajorVersion" not in context
    assert all("sqlType" in c for r in context["relations"] for c in r["columns"])
    args = with_range(
        args_for(
            "SELECT SUM(net_total) AS total FROM public.orders "
            "WHERE ordered_on >= CAST($1 AS date) AND ordered_on < CAST($2 AS date)",
            ["total"],
            context["contextId"],
        )
    )
    args["contractVersion"] = "1.2"
    for item in args["parameters"]:
        item["sqlType"] = item.pop("postgresType")
    args["outputs"][0]["sqlType"] = "numeric"
    args["semantics"]["assumptions"] = ["postgresType is literal user text"]
    original = deepcopy(args)
    result = service.call("atlas_sql_finalize", args, principal)
    assert result["status"] == "validated", result
    artifact = result["artifact"]
    assert artifact["contractVersion"] == "1.2"
    assert artifact["sql"] == args["sql"]
    assert artifact["parameters"] == args["parameters"]
    assert artifact["outputs"][0]["sqlType"] == "numeric"
    assert artifact["semantics"] == args["semantics"]
    assert artifact["artifactHash"] == digest(
        {k: v for k, v in artifact.items() if k not in {"artifactHash", "validation"}}
    )
    assert args == original
    args["outputs"][0]["sqlType"] = "text"
    rejected = service.call("atlas_sql_finalize", args, principal)
    assert rejected["error"]["code"] == "OUTPUT_CONTRACT_MISMATCH"


def test_neutral_sqlserver(tsql_catalog, principal):
    service = SqlService(MemoryCatalog(tsql_catalog), SqlServerAnalyzer())
    context = context_for(service, principal, contractVersion="1.2")
    assert context["dialect"] == "sqlserver"
    assert context["databaseMajorVersion"] == 16
    args = args_for("SELECT SUM(amount) AS total FROM dbo.orders", ["total"], context["contextId"])
    args["contractVersion"] = "1.2"
    args["outputs"][0]["sqlType"] = "decimal(38,2)"
    result = service.call("atlas_sql_finalize", args, principal)
    assert result["status"] == "validated", result
    assert result["artifact"]["outputs"][0]["sqlType"] == "decimal(38,2)"
    assert result["artifact"]["validation"]["syntax"] == "passed_static_subset"


def test_monthly_sales_arbitrary_schema_and_connection(principal):
    from atlas_sql_mcp.domain import Catalog, Column, Constraint, Relation

    catalog = Catalog(
        "arbitrary-connection",
        18,
        (
            Relation(
                "tenant_data",
                "transactions",
                (
                    Column("id", "bigint", False),
                    Column("company_id", "bigint", False),
                    Column("order_date", "date", False),
                    Column("currency_code", "character(3)", False),
                    Column("total_amount", "numeric(14,2)", False),
                ),
                (Constraint("transactions_pk", "p", ("id",)),),
            ),
        ),
        "policy",
    )
    service = SqlService(MemoryCatalog(catalog), PostgresAnalyzer())
    context = context_for(
        service, principal, connectionId="arbitrary-connection", contractVersion="1.2"
    )
    sql = """SELECT CAST(date_trunc('month', CAST(s.order_date AS timestamp)) AS date) AS sales_month,
    s.company_id AS company_id, s.currency_code AS currency_code,
    count(s.id) AS order_count, sum(s.total_amount) AS total_sales
    FROM tenant_data.transactions AS s
    GROUP BY CAST(date_trunc('month', CAST(s.order_date AS timestamp)) AS date), s.company_id, s.currency_code
    ORDER BY sales_month, company_id, currency_code"""
    args = args_for(
        sql,
        ["sales_month", "company_id", "currency_code", "order_count", "total_sales"],
        context["contextId"],
    )
    args["contractVersion"] = "1.2"
    args["semantics"]["grain"] = ["sales_month", "company_id", "currency_code"]
    result = service.call("atlas_sql_finalize", args, principal)
    assert result["status"] == "validated", result
    assert result["artifact"]["sql"] == sql
    assert result["artifact"]["validation"]["executionPerformed"] is False


def test_contract_error_guidance_does_not_echo_values():
    args = args_for("SELECT id FROM public.orders", ["id"])
    args["explanation"] = "secret-value"
    with pytest.raises(SqlError) as caught:
        validate(SCHEMAS["atlas_sql_finalize"][0], args)
    assert "Remove undeclared fields" in str(caught.value)
    assert "secret-value" not in str(caught.value)
