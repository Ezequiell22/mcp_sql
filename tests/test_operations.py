"""Operational diagnostics must remain useful without retaining caller data."""

import json
import logging

from conftest import context_for
from test_connections import session_reader as session_reader

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


def test_nested_deadlines_never_extend_the_outer_request(monkeypatch):
    import pytest

    from atlas_sql_mcp import budget
    from atlas_sql_mcp.domain import SqlError

    now = [100.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: now[0])
    with budget.request_budget(20):
        now[0] += 18
        with budget.request_budget(5):
            assert budget.remaining(5) == 2
        assert budget.remaining(20) == 2
        with budget.request_budget(1):
            now[0] += 1.1
            with pytest.raises(SqlError, match="time budget"):
                budget.remaining(5)
        assert 0 < budget.remaining(20) < 1
    assert budget.remaining(5) == 5


def test_discovery_connect_timeout_is_classified_without_leaking_driver_data(monkeypatch):
    import psycopg
    import pytest
    from test_connections import URI

    from atlas_sql_mcp import budget
    from atlas_sql_mcp.connection_strings import parse_connection
    from atlas_sql_mcp.domain import SqlError
    from atlas_sql_mcp.metadata import discovery

    now = [100.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: now[0])

    def delayed_connect(**kwargs):
        assert budget.remaining(20) == 5
        now[0] += 6
        raise psycopg.OperationalError("synthetic-private-driver-password")

    monkeypatch.setattr(discovery.psycopg, "connect", delayed_connect)
    with budget.request_budget(20), pytest.raises(SqlError) as caught:
        discovery.discover(parse_connection(URI))
    assert caught.value.code == "VALIDATION_TIMEOUT"
    assert "synthetic-private-driver-password" not in str(caught.value)


def test_separate_deployments_do_not_accept_each_others_connection_handles(
    session_reader, principal
):
    import pytest
    from test_connections import URI

    from atlas_sql_mcp.application.connections import SessionCatalog
    from atlas_sql_mcp.domain import SqlError

    first, _ = session_reader
    second = SessionCatalog()
    # HTTP instances use the same principal; their credential stores must still differ.
    principal = Principal("shared", "anonymous")
    connected = first.connect({"connectionString": URI}, principal)
    with pytest.raises(SqlError) as caught:
        second.load(principal, connected["connectionId"])
    assert caught.value.code == "CONNECTION_EXPIRED"
    assert not second._sessions


def test_discovery_does_not_connect_when_outer_deadline_cannot_cover_libpq_minimum(monkeypatch):
    import pytest
    from test_connections import URI

    from atlas_sql_mcp import budget
    from atlas_sql_mcp.connection_strings import parse_connection
    from atlas_sql_mcp.domain import SqlError
    from atlas_sql_mcp.metadata import discovery

    now = [100.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: now[0])

    def forbidden_connect(**kwargs):
        pytest.fail("An exhausted outer budget must prevent a new database connection")

    monkeypatch.setattr(discovery.psycopg, "connect", forbidden_connect)
    with budget.request_budget(1), pytest.raises(SqlError) as caught:
        discovery.discover(parse_connection(URI))
    assert caught.value.code == "VALIDATION_TIMEOUT"
    with budget.request_budget(4):
        now[0] += 1
        assert parse_connection(URI).connect_kwargs()["connect_timeout"] == 3
