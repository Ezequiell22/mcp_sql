import json
from copy import deepcopy

import pytest

from atlas_sql_mcp.config import allowed_connection_endpoints, load_connections, validate_public_url
from atlas_sql_mcp.domain import Principal, SqlError


@pytest.fixture
def config():
    return {
        "connections": [
            {
                "id": "sales",
                "host": "127.0.0.1",
                "port": 5432,
                "dbname": "sales",
                "user": "reader",
                "passwordEnv": "DB_PASSWORD",
                "sslmode": "disable",
                "grants": {"local#analyst": {"public.orders": ["id", "amount"]}},
            }
        ]
    }


def load(tmp_path, config):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(config))
    return load_connections(path)


def test_explicit_grants_and_secret_reference(tmp_path, config, monkeypatch):
    connection = load(tmp_path, config)["sales"]
    assert connection.authorized(Principal("local", "analyst")) == {
        "public.orders": ("amount", "id")
    }
    monkeypatch.setenv("DB_PASSWORD", "never-return-this")
    assert connection.connect_kwargs()["password"] == "never-return-this"
    assert "never-return-this" not in repr(connection)
    with pytest.raises(SqlError):
        connection.authorized(Principal("other", "analyst"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["connections"][0].update(password="plaintext"),
        lambda c: c["connections"][0].update(host="remote.example.test"),
        lambda c: c["connections"][0].update(sslmode="prefer"),
        lambda c: c["connections"][0].update(sslmode=[]),
        lambda c: c["connections"][0].update(port=True),
        lambda c: c["connections"][0].update(passwordEnv="not a variable"),
        lambda c: c["connections"][0].update(grants={"local#analyst": {"public.orders": ["*"]}}),
        lambda c: c["connections"][0].update(grants={}),
        lambda c: c["connections"].append(deepcopy(c["connections"][0])),
    ],
)
def test_invalid_operator_configuration(tmp_path, config, mutate):
    mutate(config)
    with pytest.raises(ValueError):
        load(tmp_path, config)


def test_remote_tls_required(tmp_path, config):
    config["connections"][0].update(host="database.example.test", sslmode="verify-full")
    assert load(tmp_path, config)["sales"].sslmode == "verify-full"


@pytest.mark.parametrize(
    "url",
    [
        "http://issuer.example.test",
        "https://user:password@issuer.example.test",
        "https://issuer.example.test/#fragment",
        "https://issuer.example.test/?secret=value",
        "https://issuer.example.test:0",
        "https://issuer.example.test:70000",
        "https://issuer.example.test:bad",
        "https://issuer.example.test\n",
        "https://issuer.example.test/has space",
    ],
)
def test_auth_urls_reject_insecure_or_credential_values(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


@pytest.mark.parametrize("value", [None, [], 1, "not an object", {"connections": [None]}])
def test_malformed_json_shapes_fail_as_configuration_errors(tmp_path, value):
    with pytest.raises(ValueError):
        load(tmp_path, value)


def test_duplicate_config_keys_are_rejected(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text('{"connections": [], "connections": []}')
    with pytest.raises(ValueError, match="Duplicate"):
        load_connections(path)


@pytest.mark.parametrize(
    "host", ["host,other", "host port=5432", "/tmp/socket", "host;secret", "host\x00"]
)
def test_configured_hosts_cannot_forward_connection_options(tmp_path, config, host):
    config["connections"][0].update(host=host, sslmode="verify-full")
    with pytest.raises(ValueError):
        load(tmp_path, config)


def test_operator_ids_cannot_shadow_expired_sessions(tmp_path, config):
    config["connections"][0]["id"] = "session-private"
    with pytest.raises(ValueError):
        load(tmp_path, config)


@pytest.mark.parametrize(
    "value", ["db", "db:0", "db:65536", "https://db:5432", "db:port", "db:５４３２"]
)
def test_invalid_endpoint_allowlist_fails_before_serving(value):
    with pytest.raises(ValueError):
        allowed_connection_endpoints(value)


def test_endpoint_allowlist_supports_ipv6_and_empty_configuration():
    assert allowed_connection_endpoints(" db.example.test:5432,::1:1433 ") == {
        "db.example.test:5432",
        "::1:1433",
    }
    assert allowed_connection_endpoints("") == set()
