"""Server configuration. Secrets are loaded locally, never accepted as tool input."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url


DIALECTS = {
    "postgresql": {"driver": "postgresql+psycopg", "parser": "postgres"},
    "mssql": {"driver": "mssql+pyodbc", "parser": "tsql"},
    "mysql": {"driver": "mysql+pymysql", "parser": "mysql"},
    # SQLite is used only by injected, local regression fixtures.
    "sqlite": {"driver": None, "parser": "sqlite"},
}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str = "deepseek-flash"

    @classmethod
    def load(cls):
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
        database_url = os.getenv("DATABASE_URL", "")
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not database_url or not api_key:
            raise ValueError("Set DATABASE_URL and DEEPSEEK_API_KEY in .env or the environment.")
        try:
            url = make_url(database_url)
            if url.drivername not in {spec["driver"] for spec in DIALECTS.values() if spec["driver"]}:
                raise ValueError
            if not all((url.host, url.database, url.username, url.password)):
                raise ValueError
            if url.drivername == "mssql+pyodbc":
                options = {k.lower(): v for k, v in url.query.items()}
                if "odbc_connect" in options or len(options) != len(url.query):
                    raise ValueError
                if options.get("driver") != "ODBC Driver 18 for SQL Server":
                    raise ValueError
                if options.get("encrypt", "yes").lower() not in {"yes", "mandatory", "strict"}:
                    raise ValueError
                if options.get("trustservercertificate", "no").lower() != "no":
                    raise ValueError
            elif url.drivername == "postgresql+psycopg":
                if {key.lower() for key in url.query} & {"host", "hostaddr"}:
                    raise ValueError
                if url.host not in {"localhost", "127.0.0.1", "::1"}:
                    if url.query.get("sslmode") != "verify-full":
                        raise ValueError
            else:
                # TLS settings are application-owned; prohibit connection-file,
                # socket, host and initialization overrides in a MySQL URL.
                if not set(url.query) <= {"ssl_ca", "charset"}:
                    raise ValueError
                if any(not isinstance(value, str) or not value for value in url.query.values()):
                    raise ValueError
        except Exception:
            raise ValueError(
                "Invalid DATABASE_URL; use the documented driver and verified TLS."
            ) from None
        return cls(
            database_url,
            api_key,
            os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        )
