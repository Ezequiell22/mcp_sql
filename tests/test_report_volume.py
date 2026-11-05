"""Result cardinality is independent of SQL text/resource budgets."""

import asyncio
import os
import sys

import psycopg
import pytest
from conftest import args_for
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_postgres import postgres as postgres  # Disposable fixture re-export for pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOW_SYNTHETIC_DB_WRITES") != "1",
    reason="Requires disposable Docker fixture write authorization",
)


def test_million_row_report_preserved_and_top_n_only_when_explicit(postgres):
    with psycopg.connect(**postgres["admin"], autocommit=True) as admin:
        admin.execute("CREATE TABLE public.volume_events(id integer PRIMARY KEY)")
        admin.execute("INSERT INTO public.volume_events SELECT generate_series(1,1000000)")
        admin.execute("GRANT SELECT ON public.volume_events TO atlas_reader")

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "atlas_sql_mcp.transport.server"]
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                connected = (
                    await session.call_tool(
                        "atlas_sql_connect",
                        {
                            "connectionString": f"postgresql://atlas_reader:test-fixture-only@127.0.0.1:{postgres['config'].port}/postgres",
                        },
                    )
                ).structured_content
                assert connected["status"] == "connected", connected
                for request, query, expected_rows in (
                    (
                        "Return every event ID without limiting results",
                        "SELECT id FROM public.volume_events",
                        1000000,
                    ),
                    (
                        "Return the ten smallest event IDs",
                        "SELECT id FROM public.volume_events ORDER BY id LIMIT 10",
                        10,
                    ),
                ):
                    context = (
                        await session.call_tool(
                            "atlas_sql_context_get",
                            {
                                "contractVersion": "1.0",
                                "connectionId": connected["connectionId"],
                                "request": request,
                            },
                        )
                    ).structured_content
                    assert context["status"] == "context", context
                    result = (
                        await session.call_tool(
                            "atlas_sql_finalize",
                            args_for(
                                query,
                                ["id"],
                                context["contextId"],
                            ),
                        )
                    ).structured_content
                    assert result["status"] == "validated", result
                    assert result["artifact"]["sql"] == query
                    assert result["artifact"]["validation"]["executionPerformed"] is False
                    # Test harness alone checks cardinality; the product never executes reports.
                    with psycopg.connect(**postgres["admin"], autocommit=True) as admin:
                        count = admin.execute(
                            "SELECT count(*) FROM (" + result["artifact"]["sql"] + ") report"
                        ).fetchone()[0]
                    assert count == expected_rows

    asyncio.run(run())
