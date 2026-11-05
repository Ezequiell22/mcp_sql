"""Read-only MCP acceptance harness for private, operator-supplied ERP scenarios.

Never executes a report or writes to a destination. Uses no built-in ERP knowledge.
Only status codes, counts, timing and scenario indices are printed; child stderr
is discarded because third-party diagnostics could contain credentials.
"""

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {"atlas_sql_connect", "atlas_sql_context_get", "atlas_sql_finalize"}


def error_code(payload):
    code = payload.get("error", {}).get("code", "INVALID_RESPONSE")
    return (
        code
        if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,80}", code)
        else "INVALID_RESPONSE"
    )


async def call(session, name, arguments, status):
    result = await session.call_tool(name, arguments)
    payload = result.structured_content
    if not isinstance(payload, dict):
        raise RuntimeError("INVALID_RESPONSE")
    if result.is_error or payload.get("status") != status:
        raise RuntimeError(error_code(payload))
    return payload


async def verify_target(session, target):
    connected = await call(
        session, "atlas_sql_connect", {"connectionString": target["connectionString"]}, "connected"
    )
    request = {
        "contractVersion": "1.2",
        "connectionId": connected["connectionId"],
        "request": "Inspect the accessible schema for report construction.",
        "pageSize": 100,
        "focus": {"include": []},
    }
    pages = relations = 0
    while True:
        context = await call(session, "atlas_sql_context_get", request, "context")
        pages += 1
        relations += len(context["relations"])
        cursor = context.get("nextCursor")
        if not cursor:
            break
        if pages >= 200:
            raise RuntimeError("PAGE_BUDGET_EXCEEDED")
        request.update(contextId=context["contextId"], cursor=cursor)
    scenarios = target.get("scenarios", [])
    if not scenarios:
        raise RuntimeError("NO_FINALIZATION_SCENARIOS")
    for index, scenario in enumerate(scenarios, 1):
        focused = await call(
            session,
            "atlas_sql_context_get",
            {
                "contractVersion": "1.2",
                "connectionId": connected["connectionId"],
                "request": scenario["request"],
                "focus": scenario.get("focus", {}),
            },
            "context",
        )
        proposal = {
            **scenario["proposal"],
            "contractVersion": "1.2",
            "contextId": focused["contextId"],
        }
        validated = await call(session, "atlas_sql_finalize", proposal, "validated")
        artifact = validated["artifact"]
        if artifact["sql"] != proposal["sql"] or artifact["validation"]["executionPerformed"]:
            raise RuntimeError("ARTIFACT_INVARIANT_FAILED")
        print(json.dumps({"scenario": index, "status": "validated"}), flush=True)
    return {"pages": pages, "relations": relations, "scenarios": len(scenarios)}


async def run(config):
    # This command is trusted operator configuration, never model-provided input.
    parameters = StdioServerParameters(
        command=config["command"][0], args=config["command"][1:], env=config.get("env")
    )
    failed = False
    with open(os.devnull, "w") as diagnostics:
        async with stdio_client(parameters, errlog=diagnostics) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as session:
                await session.initialize()
                if {tool.name for tool in (await session.list_tools()).tools} != EXPECTED_TOOLS:
                    raise RuntimeError("UNEXPECTED_TOOL_SET")
                for index, target in enumerate(config["targets"], 1):
                    started = time.monotonic()
                    try:
                        details = await verify_target(session, target)
                        result = {"status": "passed", **details}
                    except Exception as error:
                        failed = True
                        # Print only our fixed errors or a bounded protocol error code.
                        code = str(error) if isinstance(error, RuntimeError) else "HARNESS_FAILURE"
                        if not re.fullmatch(r"[A-Z_]{1,80}", code):
                            code = "HARNESS_FAILURE"
                        result = {"status": "failed", "code": code}
                    print(
                        json.dumps(
                            {
                                "target": index,
                                "seconds": round(time.monotonic() - started, 3),
                                **result,
                            }
                        ),
                        flush=True,
                    )
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "private_config", type=Path, help="Private JSON path; never pass credentials as arguments"
    )
    args = parser.parse_args()
    try:
        if args.private_config.stat().st_size > 8 * 1024 * 1024:
            raise ValueError
        config = json.loads(args.private_config.read_text())
        if (
            not isinstance(config["command"], list)
            or not config["command"]
            or not all(isinstance(value, str) and value for value in config["command"])
            or not isinstance(config["targets"], list)
            or not 1 <= len(config["targets"]) <= 10
        ):
            raise ValueError
        failed = asyncio.run(run(config))
    except Exception:
        # Neither the configuration nor exceptions are safe to echo.
        print(json.dumps({"status": "failed", "code": "HARNESS_SETUP_FAILED"}))
        failed = True
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
