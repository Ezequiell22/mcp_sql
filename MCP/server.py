"""One stdio MCP tool. Deploy one configured database scope per server instance."""

import logging
import json
import os
import time
import uuid
import warnings
from pathlib import Path
import sys
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

# Questions and metadata must not leave through optional LangSmith tracing.
# Set before loading .env (override=False) or constructing LangChain clients.
for tracing_flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
    os.environ[tracing_flag] = "false"

from MCP.generator import LOGGER, PROMPTS, REQUEST_ID, ClarificationNeeded, GenerationError, Generator, log_event
from MCP.settings import Settings


HEALTH_FILE = Path("/tmp/atlas-mcp/health.json")


def healthcheck():
    try:
        state = json.loads(HEALTH_FILE.read_text())
        age = time.monotonic() - state["heartbeat"]
        os.kill(state["pid"], 0)
        return 0 if 0 <= age < 5 else 1
    except (OSError, ValueError, KeyError, TypeError):
        return 1


async def heartbeat():
    while True:
        # Atomic replacement prevents health probes from observing partial JSON.
        temporary = HEALTH_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"pid": os.getpid(), "heartbeat": time.monotonic()}))
        temporary.replace(HEALTH_FILE)
        await anyio.sleep(1)


def configure_logging():
    # Library error messages can contain URLs, prompts or driver details. Only
    # our application-owned structured events reach Docker's stderr stream.
    logging.disable(logging.NOTSET)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.CRITICAL + 1)
    LOGGER.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    # Reflection warnings can contain identifiers and driver details.
    warnings.filterwarnings("ignore")


def configure_docker_stdio():
    # Protocol output goes to a private pipe, not Docker's diagnostic log stream.
    directory = Path("/tmp/atlas-mcp")
    directory.mkdir(mode=0o700)
    for descriptor, name in [(0, "input"), (1, "output")]:
        path = directory / name
        os.mkfifo(path, mode=0o600)
        pipe = os.open(path, os.O_RDWR)
        os.dup2(pipe, descriptor)
        os.close(pipe)


def create_server(settings=None):
    generator = Generator(settings or Settings.load())

    @asynccontextmanager
    async def lifespan(server):
        log_event("server.ready", stage="idle", transport="stdio", tool_count=1)
        try:
            if sys.argv[1:] == ["--docker-stdio"]:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(heartbeat)
                    try:
                        yield
                    finally:
                        tasks.cancel_scope.cancel()
            else:
                yield
        finally:
            if sys.argv[1:] == ["--docker-stdio"]:
                HEALTH_FILE.unlink(missing_ok=True)
            generator.engine.dispose()
            log_event("server.stopped", stage="shutdown")

    server = MCPServer(
        "Atlas SQL",
        lifespan=lifespan,
        log_level="CRITICAL",
        instructions=PROMPTS["mcp"],
    )

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        structured_output=True,
    )
    async def generate_sql(question: str) -> dict[str, Any]:
        """Generate a SELECT with joins, filters, grouping and calculations; never execute it."""
        token = REQUEST_ID.set(uuid.uuid4().hex)
        log_event("tool.received", stage="received", tool="generate_sql", question_bytes=len(question.encode()))
        try:
            result = await anyio.to_thread.run_sync(generator.generate, question, None, None)
            log_event("tool.completed", stage="completed", execution_performed=False)
            return result
        except ClarificationNeeded as error:
            log_event("tool.clarification_required", stage="clarification", reason=error.reason, execution_performed=False)
            return {
                "status": "needs_clarification",
                "reason": error.reason,
                "clarification": str(error),
                "execution_performed": False,
            }
        except GenerationError as error:
            log_event("tool.rejected", level=logging.WARNING, stage="rejected", error_type="GenerationError")
            raise ToolError(str(error)) from None
        except Exception as error:
            log_event("tool.failed", level=logging.ERROR, stage="failed", error_type=type(error).__name__)
            raise ToolError(
                "Generation failed; check database access and DeepSeek configuration."
            ) from None
        finally:
            REQUEST_ID.reset(token)

    return server


if __name__ == "__main__":
    if sys.argv[1:] == ["--healthcheck"]:
        sys.exit(healthcheck())
    configure_logging()
    try:
        docker_stdio = sys.argv[1:] == ["--docker-stdio"]
        if sys.argv[1:] and not docker_stdio:
            raise ValueError("Unsupported startup option")
        log_event("server.starting", stage="startup", transport="stdio", docker_stdio=docker_stdio)
        if docker_stdio:
            configure_docker_stdio()
        create_server().run(transport="stdio")
    except Exception as error:
        log_event("server.failed", level=logging.ERROR, stage="startup", error_type=type(error).__name__,
                  database_configured=bool(os.getenv("DATABASE_URL")),
                  provider_configured=bool(os.getenv("DEEPSEEK_API_KEY")))
        sys.exit(1)
