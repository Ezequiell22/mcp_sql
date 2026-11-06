"""Exercise the actual MCP wire protocol with local Python and optionally Docker."""

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
        pytest.skip("Set MCP_TEST_IMAGE to exercise the built container")
    synthetic = {
        "DATABASE_URL": "postgresql+psycopg://reader:synthetic-password@localhost:9/test",
        "DEEPSEEK_API_KEY": "synthetic-key",
    }
    if container:
        command, args = "docker", ["run", "--rm", "-i", "--read-only"]
        for key, value in synthetic.items():
            args.extend(["--env", f"{key}={value}"])
        args.append(image)
    else:
        command, args = sys.executable, ["-m", "MCP.server"]
    params = StdioServerParameters(
        command=command, args=args, env=synthetic, cwd=str(Path(__file__).resolve().parents[1])
    )

    async def check():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == ["generate_sql"]
                invalid = await session.call_tool("generate_sql", {"question": ""})
                assert invalid.is_error
                unavailable = await session.call_tool("generate_sql", {"question": "Order totals"})
                assert unavailable.is_error
                assert "synthetic-password" not in str(unavailable)
                assert "synthetic-key" not in str(unavailable)

    asyncio.run(check())


def test_single_docker_process_reconnects_and_logs_without_protocol_or_secrets():
    """Use a NEW disposable server; exercise the actual Docker diagnostic channel."""
    import json
    import subprocess
    import time
    import uuid

    image = os.getenv("MCP_TEST_IMAGE")
    if not image or os.getenv("ALLOW_SYNTHETIC_DB_WRITES") != "1":
        pytest.skip("Enable disposable Docker checks and provide MCP_TEST_IMAGE")
    name = "atlas-stdio-logging-" + uuid.uuid4().hex[:12]
    synthetic = {
        "DATABASE_URL": "postgresql+psycopg://reader:private-fixture-password@localhost:9/test",
        "DEEPSEEK_API_KEY": "private-fixture-provider-key",
    }
    command = ["docker", "run", "--rm", "-d", "--init", "--name", name,
               "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--log-driver", "json-file"]
    for key, value in synthetic.items():
        command.extend(["--env", f"{key}={value}"])
    command.extend([image, "--docker-stdio"])
    subprocess.run(command, check=True, capture_output=True)
    try:
        deadline = time.monotonic() + 20
        while subprocess.run(["docker", "exec", name, "test", "-p", "/tmp/atlas-mcp/output"], capture_output=True).returncode:
            assert time.monotonic() < deadline, "Disposable stdio server did not become ready"
            time.sleep(0.1)
        before = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Pid}}", name], text=True).strip()
        bridge = 'cat /tmp/atlas-mcp/output & reader=$!; trap \'kill "$reader" 2>/dev/null || true\' EXIT; cat > /tmp/atlas-mcp/input'
        params = StdioServerParameters(command="docker", args=["exec", "-i", name, "flock", "-n", "-E", "75", "/tmp/atlas-mcp/client.lock", "sh", "-c", bridge])

        async def request(question):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert set(tools.tools[0].input_schema["properties"]) == {"question"}
                    result = await session.call_tool("generate_sql", {"question": question})
                    assert result.is_error
                    # The same running server cannot be connected by a second client.
                    locked = subprocess.run(["docker", "exec", name, "flock", "-n", "-E", "75", "/tmp/atlas-mcp/client.lock", "true"], capture_output=True)
                    assert locked.returncode == 75

        asyncio.run(request("private-business-question-marker"))
        asyncio.run(request(""))
        after = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Pid}}", name], text=True).strip()
        assert before == after
        captured = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=True)
        rendered = captured.stdout + captured.stderr
        protected = list(synthetic.values()) + ["private-business-question-marker", '"jsonrpc"', "Use generate_sql to translate"]
        leaked = any(value in rendered for value in protected)
        assert not leaked, "Protocol or protected content appeared in Docker logs"
        entries = [json.loads(line) for line in rendered.splitlines() if line.startswith("{")]
        received = [entry for entry in entries if entry["event"] == "tool.received"]
        assert len(received) == 2 and len({entry["request_id"] for entry in received}) == 2
        failures = [entry for entry in entries if entry["event"] == "generation.failed"]
        assert {entry["stage"] for entry in failures} == {"validation", "metadata_discovery"}
        assert {entry["request_id"] for entry in failures} == {entry["request_id"] for entry in received}
        probe = ["docker", "exec", name, "python", "-m", "MCP.server", "--healthcheck"]
        assert subprocess.run(probe, capture_output=True).returncode == 0
        signal_server = "import json, os, signal; p=json.load(open('/tmp/atlas-mcp/health.json'))['pid']; os.kill(p, signal.SIG%s)"
        subprocess.run(["docker", "exec", name, "python", "-c", signal_server % "STOP"], check=True, capture_output=True)
        try:
            time.sleep(6)
            assert subprocess.run(probe, capture_output=True).returncode == 1
        finally:
            subprocess.run(["docker", "exec", name, "python", "-c", signal_server % "CONT"], check=True, capture_output=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)
