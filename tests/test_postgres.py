"""New disposable PostgreSQL containers only; never load a business DSN."""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from mcp.client.stdio import stdio_client
from sqlalchemy.exc import DBAPIError

from mcp import ClientSession, StdioServerParameters
from MCP.executor import Executor, QueryError
from MCP.settings import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("ALLOW_SYNTHETIC_DB_WRITES") != "1",
    reason="Enable disposable database fixtures",
)


@pytest.fixture(params=["postgres:17-alpine", "postgres:18"])
def database(request):
    name = "atlas-select-pg-" + uuid.uuid4().hex[:12]
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=fixture-admin",
            "-p",
            "127.0.0.1::5432",
            request.param,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.check_output(["docker", "port", name, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        deadline = time.monotonic() + 50
        while True:
            try:
                admin = psycopg.connect(
                    host="127.0.0.1",
                    port=port,
                    user="postgres",
                    password="fixture-admin",
                    dbname="postgres",
                    connect_timeout=1,
                    autocommit=True,
                )
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Disposable PostgreSQL startup failed") from None
                time.sleep(0.25)
        with admin:
            admin.execute("CREATE USER reader PASSWORD 'fixture-reader'")
            admin.execute(
                "CREATE TABLE public.items(id int, amount numeric, data bytea)"
            )
            admin.execute(
                "INSERT INTO public.items SELECT n, 123456789.0123456789, decode('00ff','hex') FROM generate_series(1, 1500) AS n"
            )
            admin.execute("GRANT SELECT ON public.items TO reader")
            yield (
                f"postgresql+psycopg://reader:fixture-reader@127.0.0.1:{port}/postgres",
                admin,
            )
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)


def test_postgres_real_execution_timeout_rollback_and_mcp(database):
    url, admin = database
    executor = Executor(Settings(url, 1))
    try:
        result = executor.execute(
            "SELECT id, amount, data FROM public.items ORDER BY id"
        )
        assert result["row_count"] == 1500
        assert result["rows"][-1] == [1500, "123456789.0123456789", {"base64": "AP8="}]
        assert executor.execute("SELECT id FROM public.items ORDER BY id LIMIT 2")[
            "rows"
        ] == [[1], [2]]
        assert executor.execute("SELECT '%literal%' AS x")["rows"] == [["%literal%"]]
        assert executor.execute("SELECT * FROM public.items WHERE 1=0")["columns"] == [
            "id",
            "amount",
            "data",
        ]
        assert executor.execute(
            "WITH a AS (SELECT id FROM public.items) SELECT COUNT(*) FROM a"
        )["rows"] == [[1500]]
        # Verify database read-only transaction even if the application guard is bypassed.
        with executor.engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SHOW transaction_read_only").scalar()
                == "on"
            )
            with pytest.raises(DBAPIError):
                connection.exec_driver_sql("DELETE FROM public.items")
        # Product SELECT blocked by a real fixture lock exercises statement_timeout.
        blocker = psycopg.connect(
            host="127.0.0.1",
            port=admin.info.port,
            user="postgres",
            password="fixture-admin",
            dbname="postgres",
        )
        try:
            blocker.execute("LOCK TABLE public.items IN ACCESS EXCLUSIVE MODE")
            started = time.monotonic()
            with pytest.raises(QueryError, match="timed out"):
                executor.execute("SELECT * FROM public.items")
            assert time.monotonic() - started < 6
        finally:
            blocker.rollback()
            blocker.close()
        assert executor.execute("SELECT COUNT(*) FROM public.items")["rows"] == [[1500]]
        with pytest.raises(QueryError) as error:
            executor.execute("SELECT secret_column FROM missing_secret_table")
        assert "secret" not in str(error.value)
    finally:
        executor.engine.dispose()

    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "MCP.server"],
            env={"DATABASE_URL": url, "QUERY_TIMEOUT_SECONDS": "3"},
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "execute_select", {"sql": "SELECT COUNT(*) AS n FROM public.items"}
            )
            assert not result.is_error
            assert result.structured_content["rows"] == [[1500]]

    asyncio.run(check())
