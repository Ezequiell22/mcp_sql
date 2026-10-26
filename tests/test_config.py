import json
from copy import deepcopy

import pytest

from atlas_sql_mcp.config import load_connections, validate_public_url
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
    ],
)
def test_auth_urls_reject_insecure_or_credential_values(url):
    with pytest.raises(ValueError):
        validate_public_url(url)
