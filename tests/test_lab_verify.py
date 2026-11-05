"""Acceptance harness must use real protocol results and keep private inputs private."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "erp_verify", Path(__file__).resolve().parents[1] / "labs" / "erp" / "verify.py"
)
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class Session:
    def __init__(self, *, corrupt=False):
        self.calls = []
        self.corrupt = corrupt

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "atlas_sql_connect":
            payload = {"status": "connected", "connectionId": "private-handle"}
        elif name == "atlas_sql_context_get":
            payload = {
                "status": "context",
                "contextId": "fresh-context",
                "relations": [{}],
                "nextCursor": "page-2" if len(self.calls) == 2 else None,
            }
        else:
            payload = {
                "status": "validated",
                "artifact": {
                    "sql": "rewritten" if self.corrupt else arguments["sql"],
                    "validation": {"executionPerformed": False},
                },
            }
        return SimpleNamespace(structured_content=payload, is_error=False)


def target():
    return {
        "connectionString": "SECRET-CONNECTION",
        "scenarios": [
            {
                "request": "PRIVATE-REQUEST",
                "proposal": {"sql": "PRIVATE-SQL", "contextId": "stale"},
            }
        ],
    }


def test_harness_follows_pages_uses_fresh_context_and_does_not_print_private_inputs(capsys):
    session = Session()
    result = asyncio.run(harness.verify_target(session, target()))
    assert result == {"pages": 2, "relations": 2, "scenarios": 1}
    assert session.calls[1][1]["focus"] == {"include": []}
    assert session.calls[2][1]["cursor"] == "page-2"
    assert session.calls[-1][1]["contextId"] == "fresh-context"
    assert capsys.readouterr().out == '{"scenario": 1, "status": "validated"}\n'


def test_harness_rejects_sql_rewriting():
    with pytest.raises(RuntimeError, match="ARTIFACT_INVARIANT_FAILED"):
        asyncio.run(harness.verify_target(Session(corrupt=True), target()))


def test_harness_requires_finalization_evidence():
    with pytest.raises(RuntimeError, match="NO_FINALIZATION_SCENARIOS"):
        asyncio.run(harness.verify_target(Session(), {"connectionString": "SECRET"}))


@pytest.mark.parametrize("code", ["password=SECRET", "SERVER_ERROR\nSECRET", None, {}, "A" * 81])
def test_error_output_never_trusts_freeform_server_diagnostics(code):
    assert harness.error_code({"error": {"code": code}}) == "INVALID_RESPONSE"
