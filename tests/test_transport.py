import asyncio
import getpass
import sys

import httpx2 as httpx
import pytest
from conftest import args_for
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from test_connections import session_reader as session_reader

from atlas_sql_mcp.transport.server import build_http_app, catalog_lifecycle

RESOURCE = "https://sql.example.test/mcp"


@pytest.mark.parametrize("contract_version", ["1.0", "1.2"])
def test_shared_http_without_authentication(service, monkeypatch, contract_version):
    monkeypatch.setenv("ATLAS_SQL_PUBLIC_URL", RESOURCE)
    monkeypatch.delenv("ATLAS_SQL_ISSUER", raising=False)
    monkeypatch.delenv("ATLAS_SQL_JWKS_URL", raising=False)

    async def run():
        app = build_http_app(service)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://sql.example.test"
            ) as client:
                assert (await client.get("/healthz")).status_code == 200
                assert (
                    await client.get("/.well-known/oauth-protected-resource/mcp")
                ).status_code == 404
                async with streamable_http_client(RESOURCE, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        ctx = await session.call_tool(
                            "atlas_sql_context_get",
                            {
                                "contractVersion": contract_version,
                                "connectionId": "sales",
                                "request": "Orders",
                            },
                        )
                        assert not ctx.is_error, ctx
                        result = await session.call_tool(
                            "atlas_sql_finalize",
                            {
                                **args_for(
                                    "SELECT id FROM public.orders",
                                    ["id"],
                                    ctx.structured_content["contextId"],
                                ),
                                "contractVersion": contract_version,
                            },
                        )
                        assert not result.is_error, result
                        assert (
                            result.structured_content["artifact"]["validation"][
                                "executionPerformed"
                            ]
                            is False
                        )
                for headers in (
                    {"Host": "untrusted.example.test"},
                    {"Origin": "https://untrusted.example.test"},
                ):
                    assert (await client.post("/mcp", json={}, headers=headers)).status_code in {
                        400,
                        403,
                        421,
                    }

    asyncio.run(run())


def test_idle_expired_credentials_are_reaped_and_shutdown_clears_sessions(
    session_reader, principal
):
    from test_connections import URI

    reader, _ = session_reader
    now = [1000]
    reader.clock = lambda: now[0]

    async def run():
        async with catalog_lifecycle(reader, cleanup_interval=0.001):
            reader.connect({"connectionString": URI}, principal)
            now[0] += 3601
            # No catalog call triggers lazy cleanup; the transport owns the reaper.
            for _ in range(100):
                if not reader._sessions:
                    break
                await asyncio.sleep(0.001)
            assert not reader._sessions
            reader.connect({"connectionString": URI}, principal)
            assert reader._sessions
        assert not reader._sessions
        assert reader._closed

    asyncio.run(run())


def test_stdio_real_process_registers_only_agent_tools(tmp_path):
    import json

    config = tmp_path / "connections.json"
    config.write_text(
        json.dumps(
            {
                "connections": [
                    {
                        "id": "sales",
                        "host": "127.0.0.1",
                        "port": 1,
                        "dbname": "synthetic",
                        "user": "reader",
                        "passwordEnv": "",
                        "sslmode": "disable",
                        "grants": {f"local#{getpass.getuser()}": {"public.orders": ["id"]}},
                    }
                ]
            }
        )
    )

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "atlas_sql_mcp.transport.server", "--connections", str(config)],
            env={"ATLAS_SQL_MODEL": "unsupported:must-not-load", "OPENAI_API_KEY": "unused"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert "connected agent is the only LLM" in initialized.instructions
                assert {tool.name for tool in (await session.list_tools()).tools} == {
                    "atlas_sql_connect",
                    "atlas_sql_context_get",
                    "atlas_sql_finalize",
                }
                removed = await session.call_tool("atlas_sql_generate", {})
                assert removed.is_error
                result = await session.call_tool(
                    "atlas_sql_context_get",
                    {"contractVersion": "1.0", "connectionId": "unconfigured", "request": "Orders"},
                )
                assert result.is_error
                assert result.structured_content["error"]["code"] == "CONNECTION_NOT_AUTHORIZED"

    asyncio.run(run())
