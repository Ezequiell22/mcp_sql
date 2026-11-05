"""Allowlisted operational events. Payloads, identities and exceptions never enter logs."""

import json
import logging
import secrets
import time
from contextlib import contextmanager

LOGGER = logging.getLogger("atlas_sql_mcp.operations")
TOOLS = frozenset({"atlas_sql_connect", "atlas_sql_context_get", "atlas_sql_finalize"})


@contextmanager
def operation(tool: str):
    event = {
        "event": "tool_completed",
        "requestId": secrets.token_hex(16),
        "tool": tool if tool in TOOLS else "unknown",
        "outcome": "internal_error",
    }
    started = time.monotonic()
    try:
        yield event
    finally:
        event["durationMs"] = round((time.monotonic() - started) * 1000, 3)
        level = logging.ERROR if event["outcome"] == "internal_error" else logging.INFO
        LOGGER.log(level, json.dumps(event, separators=(",", ":")))


def configure():
    handler = logging.StreamHandler()  # stderr preserves the stdio MCP wire protocol.
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.handlers = [handler]
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
