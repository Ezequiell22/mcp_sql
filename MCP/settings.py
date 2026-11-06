"""Fixed connection and query timeout; credentials never enter tool arguments."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    query_timeout_seconds: int = 30

    @classmethod
    def load(cls):
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
        database_url = os.getenv("DATABASE_URL", "")
        try:
            url = make_url(database_url)
            if url.drivername not in {"postgresql+psycopg", "mssql+pyodbc"}:
                raise ValueError
            if not all((url.host, url.database, url.username, url.password)):
                raise ValueError
            options = {k.lower(): v for k, v in url.query.items()}
            if len(options) != len(url.query) or any(
                not isinstance(v, str) for v in options.values()
            ):
                raise ValueError
            if url.drivername == "mssql+pyodbc":
                if (
                    "odbc_connect" in options
                    or options.get("driver") != "ODBC Driver 18 for SQL Server"
                ):
                    raise ValueError
                if options.get("encrypt", "yes").lower() not in {
                    "yes",
                    "mandatory",
                    "strict",
                }:
                    raise ValueError
                if options.get("trustservercertificate", "no").lower() != "no":
                    raise ValueError
            else:
                if {"host", "hostaddr", "service", "servicefile"} & options.keys():
                    raise ValueError
                if (
                    url.host not in {"localhost", "127.0.0.1", "::1"}
                    and options.get("sslmode") != "verify-full"
                ):
                    raise ValueError
        except (ArgumentError, TypeError, ValueError):
            raise ValueError(
                "Invalid DATABASE_URL; use a documented driver and verified TLS."
            ) from None
        try:
            timeout = int(os.getenv("QUERY_TIMEOUT_SECONDS", "30"))
            if not 1 <= timeout <= 86400:
                raise ValueError
        except ValueError:
            raise ValueError(
                "QUERY_TIMEOUT_SECONDS must be an integer from 1 to 86400."
            ) from None
        return cls(database_url, timeout)
