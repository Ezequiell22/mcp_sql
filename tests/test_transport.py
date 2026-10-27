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

from atlas_sql_mcp.auth import JwtVerifier
from atlas_sql_mcp.server import build_server

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


@pytest.mark.parametrize("with_generation", [False, True])
def test_shared_http_complete_flow_and_authentication(service, signing_key, with_generation):
    async def run():
        generator = None
        if with_generation:
            from test_generation import PLAN, Model, proposal

            from atlas_sql_mcp.generation import SqlGenerator

            generator = SqlGenerator(service, Model(PLAN, proposal()))
        server = build_server(service, generator=generator)
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
                        } | ({"atlas_sql_generate"} if with_generation else set())
                        assert all(
                            t.input_schema["additionalProperties"] is False and t.output_schema
                            for t in tools
                        )
                        ctx = await session.call_tool(
                            "atlas_sql_context_get",
                            {
                                "contractVersion": "1.0",
                                "connectionId": "sales",
                                "request": "Orders",
                            },
                        )
                        assert not ctx.is_error
                        ctx_id = ctx.structured_content["contextId"]
                        result = await session.call_tool(
                            "atlas_sql_finalize",
                            args_for("SELECT id FROM public.orders", ["id"], ctx_id),
                        )
                        assert not result.is_error, result
                        assert (
                            result.structured_content["artifact"]["validation"][
                                "executionPerformed"
                            ]
                            is False
                        )
                        if with_generation:
                            generated = await session.call_tool(
                                "atlas_sql_generate",
                                {
                                    "contractVersion": "1.0",
                                    "connectionId": "sales",
                                    "request": "List order IDs",
                                },
                            )
                            assert not generated.is_error, generated
                            assert generated.structured_content["status"] == "validated"
                        client.headers["Authorization"] = "Bearer " + token(
                            signing_key, sub="another-user"
                        )
                        forbidden = await session.call_tool(
                            "atlas_sql_finalize",
                            args_for("SELECT id FROM public.orders", ["id"], ctx_id),
                        )
                        assert forbidden.is_error
                        assert forbidden.structured_content["error"]["code"] == "CONTEXT_EXPIRED"

    asyncio.run(run())


def test_stdio_real_process_registers_only_generation_tools(tmp_path):
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
            args=["-m", "atlas_sql_mcp.server", "--connections", str(config)],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 2
                result = await session.call_tool(
                    "atlas_sql_context_get",
                    {"contractVersion": "1.0", "connectionId": "unconfigured", "request": "Orders"},
                )
                assert result.is_error
                assert result.structured_content["error"]["code"] == "CONNECTION_NOT_AUTHORIZED"

    asyncio.run(run())
