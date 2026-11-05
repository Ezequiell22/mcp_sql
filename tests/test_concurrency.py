"""Bounded concurrent real-database checks; not a sustained load benchmark."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import args_for
from test_postgres import postgres as postgres

from atlas_sql_mcp.analysis.worker import ProcessAnalyzer
from atlas_sql_mcp.application.connections import SessionCatalog
from atlas_sql_mcp.application.service import SqlService

pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOW_SYNTHETIC_DB_WRITES") != "1",
    reason="Requires disposable Docker fixture write authorization",
)


def test_ten_simultaneous_contexts_and_finalizations(postgres, record_property):
    service = SqlService(SessionCatalog(), ProcessAnalyzer())
    principal = postgres["principal"]
    connected = service.call(
        "atlas_sql_connect",
        {
            "connectionString": f"postgresql://atlas_reader:test-fixture-only@127.0.0.1:{postgres['config'].port}/postgres",
        },
        principal,
    )
    assert connected["status"] == "connected", connected

    def context(_):
        started = time.monotonic()
        result = service.call(
            "atlas_sql_context_get",
            {
                "contractVersion": "1.0",
                "connectionId": connected["connectionId"],
                "request": "Count orders without changing the database",
            },
            principal,
        )
        assert result["status"] == "context", result
        return result, time.monotonic() - started

    def finalize(context):
        started = time.monotonic()
        query = "SELECT COUNT(*) AS order_count FROM public.orders"
        result = service.call(
            "atlas_sql_finalize", args_for(query, ["order_count"], context["contextId"]), principal
        )
        assert result["status"] == "validated", result
        assert result["artifact"]["sql"] == query
        assert result["artifact"]["validation"]["executionPerformed"] is False
        return time.monotonic() - started

    with ThreadPoolExecutor(max_workers=10) as pool:
        contexts = list(pool.map(context, range(10)))
        finalize_seconds = list(pool.map(finalize, (value for value, _ in contexts)))
    metrics = {
        "concurrent_requests": 10,
        "context_max_seconds": round(max(seconds for _, seconds in contexts), 4),
        "finalization_max_seconds": round(max(finalize_seconds), 4),
    }
    for name, value in metrics.items():
        record_property(name, value)
    print("Concurrent fixture results: " + json.dumps(metrics, sort_keys=True))
