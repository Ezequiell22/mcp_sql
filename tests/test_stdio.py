import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp.client.stdio import stdio_client

from mcp import ClientSession, StdioServerParameters


@pytest.mark.parametrize("container", [False, True])
def test_stdio_contract(container):
    image = os.getenv("MCP_TEST_IMAGE")
    if container and not image:
        pytest.skip("Set MCP_TEST_IMAGE for container protocol checks")
    env = {
        "DATABASE_URL": "postgresql+psycopg://reader:synthetic-secret@127.0.0.1:1/test",
        "QUERY_TIMEOUT_SECONDS": "1",
    }
    command, args = sys.executable, ["-m", "MCP.server"]
    if container:
        command, args = "docker", ["run", "--rm", "-i", "--read-only"]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        args.append(image)
    params = StdioServerParameters(
        command=command,
        args=args,
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    async def check():
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert [tool.name for tool in tools] == ["execute_select"]
            assert set(tools[0].input_schema["properties"]) == {"sql"}
            assert tools[0].input_schema["required"] == ["sql"]
            for sql in ["", "DELETE FROM t", "SELECT 1; SELECT 2", "SELECT 1"]:
                result = await session.call_tool("execute_select", {"sql": sql})
                assert result.is_error
                assert "synthetic-secret" not in str(result)
            missing = await session.call_tool("execute_select", {})
            assert missing.is_error

    asyncio.run(check())


def test_persistent_docker_bridge_reconnects_and_keeps_protocol_private():
    import json
    import subprocess
    import time
    import uuid

    image = os.getenv("MCP_TEST_IMAGE")
    if not image:
        pytest.skip("Set MCP_TEST_IMAGE for the persistent bridge check")
    name = "atlas-select-bridge-" + uuid.uuid4().hex[:12]
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--init",
            "--name",
            name,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--env",
            "DATABASE_URL=postgresql+psycopg://reader:private-fixture@localhost:1/test",
            "--env",
            "QUERY_TIMEOUT_SECONDS=1",
            image,
            "--docker-stdio",
        ],
        check=True,
        capture_output=True,
    )
    try:
        deadline = time.monotonic() + 20
        while subprocess.run(
            ["docker", "exec", name, "test", "-p", "/tmp/atlas-mcp/output"],
            capture_output=True,
            check=False,
        ).returncode:
            assert time.monotonic() < deadline
            time.sleep(0.1)
        bridge = "cat /tmp/atlas-mcp/output & reader=$!; trap 'kill \"$reader\" 2>/dev/null || true' EXIT; cat > /tmp/atlas-mcp/input"
        params = StdioServerParameters(
            command="docker",
            args=[
                "exec",
                "-i",
                name,
                "flock",
                "-n",
                "-E",
                "75",
                "/tmp/atlas-mcp/client.lock",
                "sh",
                "-c",
                bridge,
            ],
        )

        async def check():
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                assert [t.name for t in (await session.list_tools()).tools] == [
                    "execute_select"
                ]
                result = await session.call_tool(
                    "execute_select",
                    {"sql": "SELECT private_business_marker FROM missing"},
                )
                assert result.is_error
                locked = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "docker",
                        "exec",
                        name,
                        "flock",
                        "-n",
                        "-E",
                        "75",
                        "/tmp/atlas-mcp/client.lock",
                        "true",
                    ],
                    capture_output=True,
                    check=False,
                )
                assert locked.returncode == 75

        asyncio.run(check())
        asyncio.run(check())
        assert (
            subprocess.run(
                ["docker", "exec", name, "python", "-m", "MCP.server", "--healthcheck"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        logs = subprocess.run(
            ["docker", "logs", name], capture_output=True, text=True, check=True
        )
        assert not any(
            value in logs.stdout + logs.stderr
            for value in ["private-fixture", "private_business_marker", "jsonrpc"]
        )
        state = json.loads(
            subprocess.check_output(
                ["docker", "exec", name, "cat", "/tmp/atlas-mcp/health.json"]
            )
        )
        assert state["pid"] > 0
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)
