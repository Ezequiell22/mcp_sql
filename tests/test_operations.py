"""Operational diagnostics must remain useful without retaining caller data."""

import json
import logging

from conftest import context_for

from atlas_sql_mcp.application.service import SqlService
from atlas_sql_mcp.domain import Principal


def test_internal_failure_has_correlated_payload_free_diagnostics(service, principal, caplog):
    secret = "postgresql://reader:private-password@private-host/private-db"

    def fail(*args):
        raise RuntimeError(secret)

    service.catalog.load = fail
    with caplog.at_level(logging.INFO, logger="atlas_sql_mcp.operations"):
        result = context_for(service, principal, request=secret)
    event = json.loads(caplog.records[-1].message)
    assert event["outcome"] == "internal_error"
    assert event["requestId"] in result["error"]["message"]
    assert event["durationMs"] >= 0
    assert set(event) == {"event", "requestId", "tool", "outcome", "durationMs"}
    assert secret not in caplog.text + json.dumps(result)
    assert "private-password" not in caplog.text
    assert "RuntimeError" not in caplog.text


def test_success_and_unknown_tool_emit_only_allowlisted_labels(service, principal, caplog):
    with caplog.at_level(logging.INFO, logger="atlas_sql_mcp.operations"):
        assert context_for(service, principal)["status"] == "context"
        service.call("private-tool-name", {}, principal)
    events = [json.loads(record.message) for record in caplog.records]
    assert [event["outcome"] for event in events] == ["success", "rejected"]
    assert events[-1]["tool"] == "unknown"
    assert "private-tool-name" not in caplog.text


def test_separate_deployments_isolate_handles_and_rate_budgets(service):
    principal = Principal("shared", "anonymous")
    other = SqlService(service.catalog, service.analyzer)
    created = context_for(service, principal)
    key = created["contextId"]
    rejected = context_for(other, principal, contextId=key)
    assert rejected["error"]["code"] == "CONTEXT_EXPIRED"
    for _ in range(60):
        service.call("atlas_sql_connect", {"connectionString": "invalid"}, principal)
    assert context_for(service, principal)["error"]["code"] == "RATE_LIMITED"
    assert context_for(other, principal)["status"] == "context"
