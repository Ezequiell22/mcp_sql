"""One SELECT execution tool over stdio, with an optional persistent Docker bridge."""

import json
import logging
import os
import sys
import time
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from MCP.executor import Executor, QueryError
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
        temporary.write_text(
            json.dumps({"pid": os.getpid(), "heartbeat": time.monotonic()})
        )
        temporary.replace(HEALTH_FILE)
        await anyio.sleep(1)


def configure_logging():
    # Raw SQLAlchemy/driver messages may contain SQL or credentials.
    logging.disable(logging.CRITICAL)
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
    executor = Executor(settings or Settings.load())

    @asynccontextmanager
    async def lifespan(server):
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
            executor.engine.dispose()

    server = MCPServer(
        "Atlas SQL",
        lifespan=lifespan,
        log_level="CRITICAL",
        instructions="Execute one read-only SELECT against the configured database. "
        "All rows are returned. Choose explicit filters or limits when needed.",
    )

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        structured_output=True,
    )
    async def execute_select(sql: str) -> dict[str, Any]:
        """Execute one SELECT unchanged; return columns, rows and row_count."""
        try:
            return await anyio.to_thread.run_sync(executor.execute, sql)
        except QueryError as error:
            raise ToolError(str(error)) from None
        except Exception:  # noqa: BLE001 - do not expose raw dependency errors.
            raise ToolError("Query execution failed.") from None

    return server


if __name__ == "__main__":
    if sys.argv[1:] == ["--healthcheck"]:
        sys.exit(healthcheck())
    configure_logging()
    try:
        docker_stdio = sys.argv[1:] == ["--docker-stdio"]
        if sys.argv[1:] and not docker_stdio:
            raise ValueError("Unsupported startup option")
        if docker_stdio:
            configure_docker_stdio()
        create_server().run(transport="stdio")
    except Exception:  # noqa: BLE001 - do not expose raw dependency errors.
        print(
            "SQL MCP startup failed; check configuration and driver installation.",
            file=sys.stderr,
        )
        sys.exit(1)
