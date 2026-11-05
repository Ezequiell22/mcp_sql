import asyncio
import getpass
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx2 as httpx
import jwt
import pytest
from conftest import args_for
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from test_connections import session_reader as session_reader

from atlas_sql_mcp.transport.auth import JwtVerifier
from atlas_sql_mcp.transport.server import build_http_app, build_server, catalog_lifecycle

ISSUER = "https://identity.example.test"
RESOURCE = "https://sql.example.test/mcp"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key, **updates):
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "sub": "analyst",
        "aud": RESOURCE,
        "exp": now + timedelta(minutes=5),
        "scope": "atlas:sql",
    }
    claims.update(updates)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def verifier(key):
    result = JwtVerifier(ISSUER, "https://identity.example.test/jwks", RESOURCE)
    result.jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())
    )
    return result


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://wrong.example.test"},
        {"aud": "another-resource"},
        {"exp": 1},
        {"sub": ""},
        {"scope": ["atlas:sql"]},
        {"exp": float("inf")},
    ],
)
def test_invalid_signed_tokens(signing_key, changes):
    assert asyncio.run(verifier(signing_key).verify_token(token(signing_key, **changes))) is None


def test_valid_token(signing_key):
    verified = asyncio.run(verifier(signing_key).verify_token(token(signing_key)))
    assert verified.subject == "analyst"
    assert verified.resource == RESOURCE
    assert verified.scopes == ["atlas:sql"]


def test_different_signing_key_rejected(signing_key):
    another_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert asyncio.run(verifier(signing_key).verify_token(token(another_key))) is None


def test_unsigned_token_rejected(signing_key):
    unsigned = jwt.encode(
        {"sub": "analyst", "iss": ISSUER, "aud": RESOURCE, "exp": 9999999999},
        key="",
        algorithm="none",
    )
    assert asyncio.run(verifier(signing_key).verify_token(unsigned)) is None


@pytest.mark.parametrize("contract_version", ["1.0", "1.2"])
def test_shared_http_complete_flow_and_authentication(service, signing_key, contract_version):
    async def run():
        server = build_server(service)
        app = server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            auth=AuthSettings(
                issuer_url=ISSUER,
                resource_server_url=RESOURCE,
                required_scopes=["atlas:sql"],
                validate_token_resource=True,
            ),
            token_verifier=verifier(signing_key),
            max_request_body_size=512 * 1024,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["sql.example.test"],
                allowed_origins=[],
            ),
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://sql.example.test"
            ) as client:
                assert (await client.post("/mcp", json={})).status_code == 401
                no_scope = await client.post(
                    "/mcp",
                    json={},
                    headers={"Authorization": "Bearer " + token(signing_key, scope="other")},
                )
                assert no_scope.status_code == 403
                discovery = await client.get("/.well-known/oauth-protected-resource/mcp")
                assert discovery.status_code == 200
                assert discovery.json()["resource"] == RESOURCE
                client.headers["Authorization"] = "Bearer " + token(signing_key)
                async with streamable_http_client(RESOURCE, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        tools = (await session.list_tools()).tools
                        assert {t.name for t in tools} == {
                            "atlas_sql_context_get",
                            "atlas_sql_finalize",
                        }
                        assert all(
                            t.input_schema["additionalProperties"] is False and t.output_schema
                            for t in tools
                        )
                        ctx = await session.call_tool(
                            "atlas_sql_context_get",
                            {
                                "contractVersion": contract_version,
                                "connectionId": "sales",
                                "request": "Orders",
                            },
                        )
                        assert not ctx.is_error
                        ctx_id = ctx.structured_content["contextId"]
                        result = await session.call_tool(
                            "atlas_sql_finalize",
                            {
                                **args_for("SELECT id FROM public.orders", ["id"], ctx_id),
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
                        client.headers["Authorization"] = "Bearer " + token(
                            signing_key, sub="another-user"
                        )
                        forbidden = await session.call_tool(
                            "atlas_sql_finalize",
                            {
                                **args_for("SELECT id FROM public.orders", ["id"], ctx_id),
                                "contractVersion": contract_version,
                            },
                        )
                        assert forbidden.is_error
                        assert forbidden.structured_content["error"]["code"] == "CONTEXT_EXPIRED"

    asyncio.run(run())


@pytest.mark.parametrize("public_url", [RESOURCE, "https://SQL.EXAMPLE.TEST:443/mcp"])
def test_runtime_http_health_auth_host_and_origin_policy(
    service, signing_key, monkeypatch, public_url
):
    from atlas_sql_mcp.transport import server as transport

    monkeypatch.setenv("ATLAS_SQL_ISSUER", ISSUER)
    monkeypatch.setenv("ATLAS_SQL_JWKS_URL", ISSUER + "/jwks")
    monkeypatch.setenv("ATLAS_SQL_PUBLIC_URL", public_url)

    def configured_verifier(issuer, jwks, resource):
        assert resource == RESOURCE
        return verifier(signing_key)

    monkeypatch.setattr(transport, "JwtVerifier", configured_verifier)

    async def run():
        app = build_http_app(service)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://sql.example.test"
            ) as client:
                health = await client.get("/healthz")
                assert health.status_code == 200 and health.text == "ok\n"
                assert health.headers["cache-control"] == "no-store"
                assert (await client.post("/mcp", json={})).status_code == 401
                metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
                assert metadata.json()["resource"] == RESOURCE
                auth = {"Authorization": "Bearer " + token(signing_key)}
                client.headers.update(auth)
                async with streamable_http_client(RESOURCE, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                for headers in [
                    {**auth, "Host": "untrusted.example.test"},
                    {**auth, "Origin": "https://untrusted.example.test"},
                ]:
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
