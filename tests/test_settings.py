import pytest

from MCP.settings import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr("MCP.settings.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("QUERY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://reader:secret@localhost/db",
        "postgresql+psycopg://reader:secret@db.example/db?sslmode=verify-full",
        "mssql+pyodbc://reader:secret@db.example/db?driver=ODBC+Driver+18+for+SQL+Server",
    ],
)
def test_configuration_needs_no_provider(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    settings = Settings.load()
    assert settings.query_timeout_seconds == 30
    assert "secret" not in repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "bad-private-secret",
        "sqlite:///x.db",
        "mysql+pymysql://r:s@host/db",
        "postgresql+psycopg://reader:secret@db.example/db",
        "postgresql+psycopg://reader:secret@localhost/db?host=remote",
        "postgresql+psycopg://reader:secret@localhost/db?service=unsafe",
        "mssql+pyodbc://reader:secret@host/db?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes",
        "mssql+pyodbc://reader:secret@host/db?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no",
        "mssql+pyodbc://reader:secret@host/db?odbc_connect=private-secret",
    ],
)
def test_invalid_configuration_is_sanitized(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ValueError, match="Invalid DATABASE_URL") as error:
        Settings.load()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("timeout", ["0", "-1", "86401", "abc", "nan", "1.5"])
def test_invalid_timeout(monkeypatch, timeout):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://r:s@localhost/db")
    monkeypatch.setenv("QUERY_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ValueError, match="QUERY_TIMEOUT_SECONDS"):
        Settings.load()
