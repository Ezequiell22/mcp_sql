import json
from copy import deepcopy
from dataclasses import replace

import pytest
from conftest import args_for, context_for, with_range
from jsonschema import Draft202012Validator

from atlas_sql_mcp.analysis.worker import ProcessAnalyzer
from atlas_sql_mcp.contracts import SCHEMAS
from atlas_sql_mcp.domain import Principal


def finalize(service, principal, sql="SELECT id FROM public.orders", outputs=("id",)):
    context = context_for(service, principal)
    assert context["status"] == "context", context
    return service.call(
        "atlas_sql_finalize", args_for(sql, outputs, context["contextId"]), principal
    )


def test_portable_artifact_and_stable_hash(service, principal):
    first = finalize(service, principal)
    second = finalize(service, principal)
    assert first["status"] == "validated", first
    a, b = first["artifact"], second["artifact"]
    assert a["artifactHash"] == b["artifactHash"]
    assert a["sql"] == "SELECT id FROM public.orders"
    assert a["filters"] == []
    assert a["validation"]["executionPerformed"] is False
    assert a["requiredSessionSettings"] == {"search_path": "pg_catalog"}
    assert a["outputs"][0]["lineage"] == ["public.orders.id"]
    # Discovery + two fresh metadata checks per finalization. No SQL API exists.
    assert len(service.catalog.calls) == 6


def test_range_binding(service, principal):
    ctx = context_for(service, principal)
    args = with_range(
        args_for(
            "SELECT id FROM public.orders WHERE ordered_on >= $1::date AND ordered_on < $2::date",
            ["id"],
            ctx["contextId"],
        )
    )
    result = service.call("atlas_sql_finalize", args, principal)
    assert result["status"] == "validated", result
    assert len(result["artifact"]["filters"]) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a: a["parameters"].pop(),
        lambda a: a["parameters"][1].update(position=1),
        lambda a: a["parameters"][0].update(postgresType="text"),
        lambda a: a["parameters"][0]["binding"].update(filterId="unknown"),
        lambda a: a["filters"][0]["fields"]["from"].update(parameterPosition=2),
        lambda a: a["filters"][0]["fields"]["from"].update(inclusive=False),
        lambda a: a["filters"][0]["fields"]["until"].update(default="2026-02-30"),
        lambda a: a["filters"][0].update(required=False),
        lambda a: a["filters"].append(deepcopy(a["filters"][0])),
        lambda a: a.update(sql=a["sql"].replace(">= $1", "> $1")),
        lambda a: a["outputs"][0].update(postgresType="text"),
        lambda a: a["outputs"][0].update(key="missing"),
        lambda a: a["semantics"].update(grain=["missing"]),
    ],
)
def test_invalid_contract_never_returns_artifact(service, principal, mutation):
    ctx = context_for(service, principal)
    args = with_range(
        args_for(
            "SELECT id FROM public.orders WHERE ordered_on >= $1::date AND ordered_on < $2::date",
            ["id"],
            ctx["contextId"],
        )
    )
    mutation(args)
    result = service.call("atlas_sql_finalize", args, principal)
    assert result["status"] == "error", result
    assert "artifact" not in result


def test_context_cannot_cross_identity(service, principal):
    context = context_for(service, principal)
    other = Principal("other-issuer", principal.subject)
    result = service.call(
        "atlas_sql_finalize",
        args_for("SELECT id FROM public.orders", ["id"], context["contextId"]),
        other,
    )
    assert result["error"]["code"] == "CONTEXT_EXPIRED"


def test_schema_drift_and_permission_revocation(service, principal):
    context = context_for(service, principal)
    service.catalog.value = replace(service.catalog.value, policy_version="revoked")
    result = service.call(
        "atlas_sql_finalize",
        args_for("SELECT id FROM public.orders", ["id"], context["contextId"]),
        principal,
    )
    assert result["error"]["code"] == "CONTEXT_STALE"


def test_expiration(service, principal):
    now = [1000.0]
    service.clock = lambda: now[0]
    ctx = context_for(service, principal)
    assert service.state.get(ctx["contextId"], principal).expires == 1600
    now[0] += 601
    result = service.call(
        "atlas_sql_finalize",
        args_for("SELECT id FROM public.orders", ["id"], ctx["contextId"]),
        principal,
    )
    assert result["error"]["code"] == "CONTEXT_EXPIRED"


def test_cursor_binding_and_coverage(service, principal):
    first = context_for(service, principal, pageSize=1)
    assert first["coverage"] == "partial"
    assert first["nextCursor"]
    second = context_for(
        service, principal, pageSize=1, contextId=first["contextId"], cursor=first["nextCursor"]
    )
    assert first["relations"][0]["name"] != second["relations"][0]["name"]
    bad = context_for(
        service, principal, pageSize=2, contextId=first["contextId"], cursor=first["nextCursor"]
    )
    assert bad["status"] == "error"
    assert (
        context_for(service, principal, contextId=first["contextId"], request="Another request")[
            "error"
        ]["code"]
        == "CONTEXT_STALE"
    )


def test_focus_cannot_disclose_unknown_objects(service, principal):
    result = context_for(service, principal, focus={"relations": ["private.secrets"]})
    assert result["error"]["code"] == "OBJECT_NOT_AUTHORIZED"
    assert "private.secrets" not in json.dumps(result)


def test_unknown_nested_fields_rejected(service, principal):
    assert (
        context_for(service, principal, focus={"sql": "select secret"})["error"]["code"]
        == "INVALID_CONTRACT"
    )


def test_input_size(service, principal):
    args = {"contractVersion": "1.0", "connectionId": "sales", "request": "x" * (600 * 1024)}
    assert (
        service.call("atlas_sql_context_get", args, principal)["error"]["code"] == "INPUT_TOO_LARGE"
    )


def test_real_isolated_analyzer(catalog):
    result = ProcessAnalyzer().analyze("SELECT id FROM public.orders", catalog)
    assert result["outputs"][0]["key"] == "id"


def test_schemas_are_valid():
    for schemas in SCHEMAS.values():
        for schema in schemas:
            Draft202012Validator.check_schema(schema)


def test_rate_and_context_bounds(service, principal):
    for _ in range(25):
        assert context_for(service, principal)["status"] == "context"
    assert context_for(service, principal)["error"]["code"] == "RATE_LIMITED"


def test_response_is_never_silently_truncated(service, principal, monkeypatch):
    from atlas_sql_mcp.application import service as service_module

    monkeypatch.setattr(service_module, "OUTPUT_BYTES", 1024)
    result = context_for(service, principal)
    assert result["error"]["code"] == "OUTPUT_TOO_LARGE"
    assert "relations" not in result


def test_actual_range_predicate_direction(service, principal):
    ctx = context_for(service, principal)
    sql = "SELECT id FROM public.orders WHERE $1::date <= ordered_on AND $2::date > ordered_on"
    args = with_range(args_for(sql, ["id"], ctx["contextId"]))
    assert service.call("atlas_sql_finalize", args, principal)["status"] == "validated"


def test_exact_large_integer_default_is_preserved(service, principal):
    ctx = context_for(service, principal)
    args = args_for(
        "SELECT id FROM public.orders WHERE quantity >= $1::bigint", ["id"], ctx["contextId"]
    )
    args["parameters"] = [
        {
            "position": 1,
            "name": "quantity",
            "postgresType": "bigint",
            "nullable": False,
            "binding": {"filterId": "quantity", "field": "value"},
        }
    ]
    args["filters"] = [
        {
            "id": "quantity",
            "label": "Quantidade",
            "kind": "integer",
            "required": False,
            "fields": {
                "value": {
                    "type": "integer",
                    "nullable": False,
                    "parameterPosition": 1,
                    "default": "9007199254740993",
                }
            },
        }
    ]
    result = service.call("atlas_sql_finalize", args, principal)
    assert result["status"] == "validated", result
    assert result["artifact"]["filters"][0]["fields"]["value"]["default"] == "9007199254740993"
    args["filters"][0]["fields"]["value"]["default"] = "9223372036854775808"
    assert (
        service.call("atlas_sql_finalize", args, principal)["error"]["code"] == "PARAMETER_MISMATCH"
    )


def test_worker_timeout_is_explicit(catalog, monkeypatch):
    import subprocess

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("isolated-parser", 7)

    monkeypatch.setattr(subprocess, "run", timeout)
    from atlas_sql_mcp.domain import SqlError

    with pytest.raises(SqlError) as error:
        ProcessAnalyzer().analyze("SELECT id FROM public.orders", catalog)
    assert error.value.code == "VALIDATION_TIMEOUT"


def test_range_cannot_claim_unrelated_columns(service, principal):
    ctx = context_for(service, principal)
    sql = (
        "SELECT id FROM public.orders WHERE ordered_on >= $1::date AND created_at::date < $2::date"
    )
    args = with_range(args_for(sql, ["id"], ctx["contextId"]))
    assert (
        service.call("atlas_sql_finalize", args, principal)["error"]["code"] == "PARAMETER_MISMATCH"
    )


def test_expired_request_budget_stops_adapters():
    from atlas_sql_mcp.budget import remaining, request_budget
    from atlas_sql_mcp.domain import SqlError

    with request_budget(-1), pytest.raises(SqlError) as error:
        remaining(7)
    assert error.value.code == "VALIDATION_TIMEOUT"
    assert remaining(7) == 7


def test_request_budget_counts_utf8_bytes(service, principal):
    assert (
        context_for(service, principal, request="á" * 10000)["error"]["code"] == "INPUT_TOO_LARGE"
    )


def test_request_fixtures_match_wire_contract():
    from pathlib import Path

    from atlas_sql_mcp.contracts import validate

    directory = Path(__file__).parent / "fixtures" / "payloads"
    for tool, filename in [
        ("atlas_sql_connect", "connect.json"),
        ("atlas_sql_context_get", "context.json"),
        ("atlas_sql_finalize", "finalize.json"),
        ("atlas_sql_finalize", "finalize-neutral.json"),
        ("atlas_sql_finalize", "finalize-sqlserver.json"),
    ]:
        validate(SCHEMAS[tool][0], json.loads((directory / filename).read_text()))


@pytest.mark.parametrize(
    "predicate",
    [
        "ordered_on >= $1::date OR ordered_on < $2::date",
        "NOT (ordered_on >= $1::date AND ordered_on < $2::date)",
        "CASE WHEN ordered_on >= $1::date THEN false ELSE true END AND ordered_on < $2::date",
    ],
)
def test_range_cannot_be_bypassed_by_boolean_structure(service, principal, predicate):
    ctx = context_for(service, principal)
    args = with_range(
        args_for(f"SELECT id FROM public.orders WHERE {predicate}", ["id"], ctx["contextId"])
    )
    result = service.call("atlas_sql_finalize", args, principal)
    assert result["error"]["code"] == "PARAMETER_MISMATCH"


def test_failed_responses_do_not_consume_context_or_cursor_storage(service, principal, monkeypatch):
    from atlas_sql_mcp.application import service as service_module

    normal_limit = service_module.OUTPUT_BYTES
    monkeypatch.setattr(service_module, "OUTPUT_BYTES", 1024)
    for _ in range(25):
        rejected = context_for(service, principal, pageSize=1)
        assert rejected["error"]["code"] == "OUTPUT_TOO_LARGE"
    assert not service.state.contexts
    assert not service.state.cursors
    monkeypatch.setattr(service_module, "OUTPUT_BYTES", normal_limit)
    assert context_for(service, principal)["status"] == "context"


def test_failed_continuation_does_not_publish_an_unseen_cursor(service, principal, monkeypatch):
    from atlas_sql_mcp.application import service as service_module

    first = context_for(service, principal, pageSize=1)
    before = dict(service.state.cursors)
    monkeypatch.setattr(service_module, "OUTPUT_BYTES", 1024)
    rejected = context_for(
        service,
        principal,
        contextId=first["contextId"],
        cursor=first["nextCursor"],
        pageSize=1,
    )
    assert rejected["error"]["code"] == "OUTPUT_TOO_LARGE"
    assert service.state.cursors == before
    assert len(service.state.contexts) == 1


def test_context_expiring_during_discovery_is_never_published(service, principal):
    now = [1000.0]
    service.clock = lambda: now[0]
    original = service.catalog.load

    def delayed(*args):
        now[0] += 601
        return original(*args)

    service.catalog.load = delayed
    rejected = context_for(service, principal)
    assert rejected["error"]["code"] == "CONTEXT_EXPIRED"
    assert not service.state.contexts
    assert not service.state.cursors


def test_cursor_capacity_failure_is_atomic_with_new_context(service, principal):
    from atlas_sql_mcp.application.context import Cursor

    cursor = Cursor(principal, "existing", "focus", 1, service.clock() + 600)
    service.state.cursors = {str(i): cursor for i in range(5000)}
    rejected = context_for(service, principal, pageSize=1)
    assert rejected["error"]["code"] == "RATE_LIMITED"
    assert not service.state.contexts
    assert len(service.state.cursors) == 5000
