"""Disposable SQL Server 2022/2025, verified TLS and real restricted readers."""

import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("ALLOW_SYNTHETIC_DB_WRITES") != "1"
    or os.getenv("ACCEPT_SQLSERVER_EULA") != "Y",
    reason="Enable disposable writes and accept the SQL Server EULA",
)

RUNNER = r"""
import asyncio, os, time
import pyodbc
from MCP.executor import Executor, QueryError
from MCP.settings import Settings
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
admin = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;UID=sa;PWD=Fixture-Admin-8!;Encrypt=yes;TrustServerCertificate=yes"
deadline = time.monotonic() + 100
while True:
    try:
        connection = pyodbc.connect(admin, autocommit=True, timeout=2)
        break
    except pyodbc.Error:
        if time.monotonic() > deadline:
            raise RuntimeError("Disposable startup failed") from None
        time.sleep(1)
cursor = connection.cursor()
cursor.execute("CREATE DATABASE fixture")
cursor.execute("CREATE LOGIN reader WITH PASSWORD='Reader-test-8!', CHECK_POLICY=OFF")
cursor.execute("USE fixture")
cursor.execute("CREATE USER reader FOR LOGIN reader")
cursor.execute("CREATE TABLE dbo.items(id int PRIMARY KEY, amount decimal(20,4))")
cursor.execute("INSERT INTO dbo.items VALUES (1, 123456789.0123), (2, 2.0000)")
cursor.execute("GRANT SELECT ON dbo.items TO reader")
# Hold a write lock so the product's SELECT must hit its actual query timeout.
blocker = pyodbc.connect(admin + ";DATABASE=fixture", autocommit=False)
connection.close()
os.environ["DATABASE_URL"] = "mssql+pyodbc://reader:Reader-test-8!@localhost/fixture?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
os.environ["QUERY_TIMEOUT_SECONDS"] = "1"
executor = Executor(Settings.load())
assert executor.execute("SELECT id, amount FROM dbo.items ORDER BY id")["rows"] == [[1, "123456789.0123"], [2, "2.0000"]]
assert executor.execute("SELECT TOP (1) id FROM dbo.items ORDER BY id")["row_count"] == 1
assert executor.execute("SELECT '%literal%' AS x")["rows"] == [["%literal%"]]
assert executor.execute("SELECT * FROM dbo.items WHERE 1=0")["columns"] == ["id", "amount"]
assert executor.execute("WITH a AS (SELECT id FROM dbo.items) SELECT COUNT(*) AS n FROM a")["rows"] == [[2]]
# Confirm driver-level timeout was assigned, not just a login timeout.
with executor.engine.connect() as raw:
    assert raw.connection.driver_connection.timeout == 1
blocker.cursor().execute("UPDATE dbo.items WITH (TABLOCKX) SET amount=amount")
started = time.monotonic()
try:
    executor.execute("SELECT * FROM dbo.items")
    raise AssertionError("Expected timeout")
except QueryError:
    assert time.monotonic() - started < 8
finally:
    blocker.rollback()
    blocker.close()
assert executor.execute("SELECT COUNT(*) FROM dbo.items")["rows"] == [[2]]
# Bypass the application guard to verify actual database grants refuse writes.
with executor.engine.connect() as connection:
    try:
        connection.exec_driver_sql("DELETE FROM dbo.items")
        raise AssertionError("Reader can write")
    except pyodbc.Error:
        pass
    except Exception as error:
        from sqlalchemy.exc import DBAPIError
        assert isinstance(error, DBAPIError)
executor.engine.dispose()
async def check():
    import sys
    params = StdioServerParameters(command=sys.executable, args=["-m", "MCP.server"], env={"DATABASE_URL":os.environ["DATABASE_URL"], "QUERY_TIMEOUT_SECONDS":"3"}, cwd="/app")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert [t.name for t in tools] == ["execute_select"]
            result = await session.call_tool("execute_select", {"sql":"SELECT COUNT(*) AS n FROM dbo.items"})
            assert not result.is_error
            assert result.structured_content["rows"] == [[2]]
asyncio.run(check())
"""


@pytest.mark.parametrize("version", ["2022", "2025"])
def test_sqlserver_usage(version):
    image = os.getenv("MCP_TEST_IMAGE")
    if not image:
        pytest.fail("SQL Server checks require MCP_TEST_IMAGE with ODBC Driver 18")
    root = Path(__file__).resolve().parents[1]
    name = "atlas-sqlserver-usage-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="atlas-sqlserver-tls-") as directory:
        temporary = Path(directory)
        # Trust a dedicated CA, not the server leaf itself, so hostname checks
        # exercise a normal verified chain rather than direct leaf trust.
        commands = [
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(temporary / "ca.key"),
                "-out",
                str(temporary / "ca.crt"),
                "-days",
                "1",
                "-subj",
                "/CN=Disposable fixture CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            ],
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(temporary / "server.key"),
                "-out",
                str(temporary / "server.csr"),
                "-subj",
                "/CN=localhost",
            ],
        ]
        (temporary / "server.ext").write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        )
        commands.append(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(temporary / "server.csr"),
                "-CA",
                str(temporary / "ca.crt"),
                "-CAkey",
                str(temporary / "ca.key"),
                "-CAcreateserial",
                "-out",
                str(temporary / "server.crt"),
                "-days",
                "1",
                "-extfile",
                str(temporary / "server.ext"),
            ]
        )
        for command in commands:
            subprocess.run(command, check=True, capture_output=True)
        # Disposable key only; SQL Server runs as an unprivileged container user.
        temporary.chmod(0o755)
        (temporary / "server.key").chmod(0o644)
        (temporary / "mssql.conf").write_text(
            "[network]\ntlscert=/tls/server.crt\ntlskey=/tls/server.key\nforceencryption=1\n"
        )
        started = False
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    name,
                    "--memory",
                    "3g",
                    "-e",
                    "ACCEPT_EULA=Y",
                    "-e",
                    "MSSQL_SA_PASSWORD=Fixture-Admin-8!",
                    "-e",
                    "MSSQL_PID=Developer",
                    "-e",
                    "MSSQL_MEMORY_LIMIT_MB=2048",
                    "-v",
                    f"{temporary}:/tls:ro",
                    "-v",
                    f"{temporary / 'mssql.conf'}:/var/opt/mssql/mssql.conf:ro",
                    f"mcr.microsoft.com/mssql/server:{version}-latest",
                ],
                check=True,
                capture_output=True,
            )
            started = True
            runner = temporary / "usage.py"
            runner.write_text(RUNNER)
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    "0",
                    "--network",
                    f"container:{name}",
                    "-v",
                    f"{root}:/app:ro",
                    "-v",
                    f"{temporary}:/fixtures:ro",
                    "--entrypoint",
                    "sh",
                    image,
                    "-c",
                    "cp /fixtures/ca.crt /usr/local/share/ca-certificates/fixture.crt && update-ca-certificates >/dev/null 2>&1 && PYTHONPATH=/app python /fixtures/usage.py",
                ],
                text=True,
                capture_output=True,
                timeout=240,
                check=False,
            )
            if result.returncode:
                frames = re.findall(
                    r'File "/fixtures/usage.py", line (\d+)', result.stderr
                )
                pytest.fail(
                    "Disposable SQL Server check failed at fixture line "
                    + (frames[-1] if frames else "startup")
                )
        finally:
            if started:
                subprocess.run(
                    ["docker", "rm", "-f", name], check=True, capture_output=True
                )
