"""Deterministic host-agent scripts over real MCP stdio, not a live LLM evaluation.

All synthetic business rows and report execution belong exclusively to this
opt-in harness. The fixture starts a NEW disposable container, never a client DB.
"""

import asyncio
import json
import os
import sys
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from conftest import args_for, with_range
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_postgres import postgres  # noqa: F401 -- disposable container fixture

pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOW_SYNTHETIC_DB_WRITES") != "1",
    reason="Requires disposable Docker fixture write authorization",
)

ERP_SQL = """
CREATE SCHEMA erp;
CREATE TABLE erp.parties(id integer PRIMARY KEY,region text NOT NULL);
CREATE TABLE erp.invoices(id integer PRIMARY KEY,party_id integer REFERENCES erp.parties(id),
 issued_on date NOT NULL,status text NOT NULL);
CREATE TABLE erp.lines(id integer PRIMARY KEY,invoice_id integer REFERENCES erp.invoices(id),
 product text NOT NULL,quantity integer NOT NULL,price numeric(12,2) NOT NULL,cost numeric(12,2) NOT NULL);
CREATE TABLE erp.payments(id integer PRIMARY KEY,invoice_id integer REFERENCES erp.invoices(id),amount numeric(12,2) NOT NULL);
INSERT INTO erp.parties VALUES(1,'North'),(2,'South'),(3,'North'),(4,'West');
INSERT INTO erp.invoices VALUES(1,1,'2026-01-10','posted'),(2,1,'2026-02-10','posted'),
 (3,2,'2026-02-12','posted'),(4,3,'2026-02-14','canceled'),(5,2,'2026-03-01','posted');
INSERT INTO erp.lines VALUES(1,1,'A',2,50,30),(2,1,'B',1,100,60),(3,2,'A',2,50,30),
 (4,2,'C',2,25,10),(5,3,'B',3,100,60),(6,4,'A',10,50,30),(7,5,'C',4,25,10);
INSERT INTO erp.payments VALUES(1,1,50),(2,1,50),(3,2,150),(4,3,75),(5,3,25);
GRANT USAGE ON SCHEMA erp TO atlas_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA erp TO atlas_reader;
"""


@pytest.fixture(scope="module")
def erp_database(postgres):  # noqa: F811 -- imported pytest fixture
    with psycopg.connect(**postgres["admin"], autocommit=True) as conn:
        conn.execute(ERP_SQL)
    return postgres


BASE = (
    "FROM erp.parties p JOIN erp.invoices i ON i.party_id=p.id "
    "JOIN erp.lines l ON l.invoice_id=i.id WHERE i.status='posted' "
)
D = Decimal
# Client requests explicitly define the business measure; metadata alone cannot.
REPORTS = [
    (
        "Regional gross revenue, posted invoices only, quantity times price, all dates.",
        "SELECT p.region,SUM(l.quantity*l.price) AS revenue "
        + BASE
        + "GROUP BY p.region ORDER BY p.region",
        ["region", "revenue"],
        [],
        [("North", D(350)), ("South", D(400))],
    ),
    (
        "Product margin, posted invoices only: quantity times price minus quantity times cost.",
        "SELECT l.product,SUM(l.quantity*(l.price-l.cost)) AS margin "
        + BASE
        + "GROUP BY l.product ORDER BY l.product",
        ["product", "margin"],
        [],
        [("A", D(80)), ("B", D(160)), ("C", D(90))],
    ),
    (
        "Receivable balance per posted invoice: gross line total less payments, no join fanout.",
        "WITH billed AS (SELECT invoice_id,SUM(quantity*price) AS total FROM erp.lines GROUP BY invoice_id),"
        "paid AS (SELECT invoice_id,SUM(amount) AS total FROM erp.payments GROUP BY invoice_id) "
        "SELECT i.id,b.total-COALESCE(p.total,0) AS balance FROM erp.invoices i "
        "JOIN billed b ON b.invoice_id=i.id LEFT JOIN paid p ON p.invoice_id=i.id "
        "WHERE i.status='posted' ORDER BY i.id",
        ["id", "balance"],
        [],
        [(1, D(100)), (2, D(0)), (3, D(200)), (5, D(100))],
    ),
    (
        "Regional gross revenue for February, posted only, inclusive start and exclusive end.",
        "SELECT p.region,SUM(l.quantity*l.price) AS revenue "
        + BASE
        + "AND i.issued_on >= $1::date AND i.issued_on < $2::date GROUP BY p.region ORDER BY p.region",
        ["region", "revenue"],
        ["2026-02-01", "2026-03-01"],
        [("North", D(150)), ("South", D(300))],
    ),
    (
        "Rank customers by gross posted sales descending; ID breaks ties.",
        "WITH totals AS (SELECT i.party_id,SUM(l.quantity*l.price) AS revenue "
        + BASE
        + "GROUP BY i.party_id) SELECT party_id,revenue,ROW_NUMBER() OVER "
        "(ORDER BY revenue DESC,party_id) AS sales_rank FROM totals ORDER BY sales_rank",
        ["party_id", "revenue", "sales_rank"],
        [],
        [(2, D(400), 1), (1, D(350), 2)],
    ),
    (
        "Customers with no posted invoices, including customers with canceled invoices only.",
        "SELECT p.id FROM erp.parties p WHERE NOT EXISTS (SELECT 1 FROM erp.invoices i "
        "WHERE i.party_id=p.id AND i.status='posted') ORDER BY p.id",
        ["id"],
        [],
        [(3,), (4,)],
    ),
]


def large_erp_report():
    """Five years of independently filtered monthly ERP aggregates and rankings."""
    ctes, branches = [], []
    for year in range(2022, 2027):
        for month in range(1, 13):
            start = date(year, month, 1)
            end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
            name = f"period_{year}_{month}"
            ctes.append(f"""{name} AS (
 SELECT
  p.region,
  SUM(l.quantity*l.price) AS revenue,
  SUM(l.quantity*l.cost) AS cost,
  SUM(l.quantity*(l.price-l.cost)) AS margin,
  SUM(l.quantity) AS units,
  COUNT(DISTINCT i.id) AS invoices,
  COUNT(DISTINCT p.id) AS customers,
  MIN(l.price) AS minimum_price,
  MAX(l.price) AS maximum_price,
  AVG(l.price) AS mean_price
 FROM erp.parties p
 JOIN erp.invoices i
  ON i.party_id=p.id
 JOIN erp.lines l
  ON l.invoice_id=i.id
 WHERE i.status='posted'
  AND i.issued_on >= DATE '{start}'
  AND i.issued_on < DATE '{end}'
 GROUP BY p.region
)""")
            branches.append(f"SELECT DATE '{start}' AS month,region,revenue FROM {name}")
    ctes.append("monthly AS (" + " UNION ALL ".join(branches) + ")")
    return (
        "WITH "
        + ",\n".join(ctes)
        + " SELECT month,region,revenue FROM monthly ORDER BY month,region"
    )


LARGE_ERP_SQL = large_erp_report()
assert len(LARGE_ERP_SQL.splitlines()) > 1000
REPORTS.append(
    (
        "Five-year monthly regional posted revenue dashboard, preserving every month query.",
        LARGE_ERP_SQL,
        ["month", "region", "revenue"],
        [],
        [
            (date(2026, 1, 1), "North", D(200)),
            (date(2026, 2, 1), "North", D(150)),
            (date(2026, 2, 1), "South", D(300)),
            (date(2026, 3, 1), "South", D(100)),
        ],
    )
)


async def connect_agent(session, database):
    await session.initialize()
    connected = await session.call_tool(
        "atlas_sql_connect",
        {
            "connectionString": f"postgresql://atlas_reader:test-fixture-only@127.0.0.1:{database['config'].port}/postgres",
            "schemas": ["erp"],
        },
    )
    assert not connected.is_error, connected
    return connected.structured_content["connectionId"]


async def context_for_agent(session, key, request):
    context = await session.call_tool(
        "atlas_sql_context_get",
        {
            "contractVersion": "1.0",
            "connectionId": key,
            "request": request,
        },
    )
    assert not context.is_error, context
    return context.structured_content


@pytest.mark.parametrize(
    "request_text,query,outputs,bindings,expected",
    REPORTS,
    ids=[
        "regional-sales",
        "product-margin",
        "receivables-no-fanout",
        "typed-date-filter",
        "customer-ranking",
        "inactive-customers",
        "large-five-year-dashboard",
    ],
)
def test_agent_report_through_stdio(erp_database, request_text, query, outputs, bindings, expected):
    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "atlas_sql_mcp.transport.server"]
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                key = await connect_agent(session, erp_database)
                context = await context_for_agent(session, key, request_text)
                assert {r["name"] for r in context["relations"]} == {
                    "parties",
                    "invoices",
                    "lines",
                    "payments",
                }
                proposal = args_for(query, outputs, context["contextId"])
                if bindings:
                    proposal = with_range(proposal)
                result = await session.call_tool("atlas_sql_finalize", proposal)
                assert not result.is_error, result
                artifact = result.structured_content["artifact"]
                assert artifact["sql"] == query
                assert artifact["validation"]["executionPerformed"] is False
                assert "test-fixture-only" not in json.dumps(context) + json.dumps(artifact)
                # Independent manually reconciled totals, including repeated payments
                # and a canceled invoice. Only this harness executes the artifact.
                with psycopg.connect(**erp_database["admin"], autocommit=True) as conn:
                    with psycopg.RawCursor(conn) as cur:
                        cur.execute(artifact["sql"], bindings or None)
                        assert cur.fetchall() == expected

    asyncio.run(run())


@pytest.mark.parametrize(
    "query",
    [
        "SELECT invented_revenue FROM erp.invoices",
        "SELECT id FROM erp.customer_profitability_catalog",
        "DELETE FROM erp.invoices",
        "SELECT id FROM erp.invoices; SELECT id FROM erp.parties",
    ],
)
def test_invalid_agent_proposals_fail_without_artifact(erp_database, query):
    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "atlas_sql_mcp.transport.server"]
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                key = await connect_agent(session, erp_database)
                context = await context_for_agent(session, key, "Report posted invoices")
                result = await session.call_tool(
                    "atlas_sql_finalize", args_for(query, ["id"], context["contextId"])
                )
                assert result.is_error
                assert result.structured_content["status"] == "error"
                assert "artifact" not in result.structured_content

    asyncio.run(run())
