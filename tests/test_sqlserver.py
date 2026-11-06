"""SQL Server usage checks: disposable fixtures, verified TLS, no generated execution."""

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from dotenv import dotenv_values

pytestmark = pytest.mark.skipif(
    os.getenv("ALLOW_SYNTHETIC_DB_WRITES") != "1" or os.getenv("ACCEPT_SQLSERVER_EULA") != "Y",
    reason="Disposable SQL Server requires write and Microsoft EULA gates",
)

# This program runs in the product image, which contains Microsoft's ODBC driver.
# Only fixture administration uses a trusted-server override. Product connections
# use a locally trusted certificate and hostname verification throughout.
RUNNER = r"""
import os, time, json, re
from urllib.parse import quote
import pyodbc
with open("/etc/hosts", "a") as hosts:
    hosts.write("\n127.0.0.1 wrong.invalid\n")
from sqlalchemy import event
from MCP.generator import Generator, GenerationError, ScopeDecision, Selection
from MCP.settings import Settings
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda
class Model(FakeListChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        result.generations[0].message.response_metadata["finish_reason"] = "stop"
        return result
    def with_structured_output(self, schema, **kwargs):
        def choose(messages):
            payload = json.loads(messages[-1][1])
            if schema is ScopeDecision:
                question = payload["request"].lower()
                target = "archive" if "archived" in question else "dbo" if "anchor" in question else "erp"
                return ScopeDecision(outcome="selected", names=[target])
            referenced = {name.lower() for name in re.findall(r"(?:FROM|JOIN)\s+(?:\w+\.)?(\w+)", self.responses[self.i], re.I)}
            return Selection(names=[name for name in payload["candidates"] if name.rsplit(".", 1)[-1].lower() in referenced])
        return RunnableLambda(choose)

password = "Fixture-Admin-8!"
admin = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;UID=sa;PWD=" + password + ";Encrypt=yes;TrustServerCertificate=yes;Connection Timeout=2"
deadline = time.monotonic() + 100
while True:
    try:
        connection = pyodbc.connect(admin, autocommit=True)
        break
    except pyodbc.Error:
        if time.monotonic() > deadline:
            raise RuntimeError("Disposable SQL Server startup failed") from None
        time.sleep(1)
cursor = connection.cursor()
cursor.execute("CREATE LOGIN reader WITH PASSWORD='Reader-p@ss:/?#%+8!', CHECK_POLICY=OFF")
for database in ["erp_primary", "erp_secondary"]:
    cursor.execute("CREATE DATABASE " + database)
    cursor.execute("USE " + database)
    cursor.execute("CREATE USER reader FOR LOGIN reader")
    for schema in ["erp", "archive"]:
        cursor.execute("CREATE SCHEMA " + schema)
        cursor.execute("CREATE TABLE " + schema + ".customers(id int PRIMARY KEY, name nvarchar(100))")
        cursor.execute("CREATE TABLE " + schema + ".orders(id int PRIMARY KEY, customer_id int REFERENCES " + schema + ".customers(id), amount decimal(18,2), ordered_at date)")
        cursor.execute("GRANT SELECT ON SCHEMA::" + schema + " TO reader")
    cursor.execute("CREATE VIEW archive.order_report AS SELECT id, amount FROM archive.orders")
    cursor.execute("CREATE TABLE dbo.anchor(id int PRIMARY KEY)")
    cursor.execute("GRANT SELECT ON dbo.anchor TO reader")
connection.close()

encoded = quote("Reader-p@ss:/?#%+8!", safe="")
base = "mssql+pyodbc://reader:" + encoded + "@localhost:1433/"
options = "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
variants = [
    base + "erp_primary" + options,
    base.replace("localhost", "127.0.0.1") + "erp_secondary" + options.replace("+", "%20") + "&HostNameInCertificate=localhost",
]
for url in variants:
    os.environ["DATABASE_URL"] = url
    os.environ["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY") or "offline"
    settings = Settings.load()
    print("checking connection variant", variants.index(url), flush=True)
    try:
        probe = pyodbc.connect("DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;UID=reader;PWD={Reader-p@ss:/?#%+8!};DATABASE=erp_primary;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=3")
        probe.close()
    except pyodbc.Error as error:
        print("verified TLS connection SQLSTATE", error.args[0], flush=True)
        raise RuntimeError("Verified TLS fixture connection failed") from None
    generator = Generator(settings, model=Model(responses=[
        "SELECT id FROM erp.orders", "SELECT id FROM archive.orders", "SELECT id FROM dbo.anchor"
    ]))
    statements = []
    @event.listens_for(generator.engine, "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    try:
        for schema, table in [("erp", "orders"), ("archive", "orders"), ("dbo", "anchor")]:
            print("checking request", schema or "default", flush=True)
            result = generator.generate("Archived order IDs" if schema == "archive" else "Anchor IDs" if schema == "dbo" else "Current order IDs")
            assert result["execution_performed"] is False
            assert result["schema"] == (schema or "dbo")
        assert not any("from erp.orders" in s.lower() or "from archive.orders" in s.lower() or "from dbo.anchor" in s.lower() for s in statements)
        generator.model = Model(responses=["SELECT id FROM erp.orders"])
        try:
            generator.generate("Archived order IDs")
            raise AssertionError("Cross-request schema accepted")
        except GenerationError:
            pass
    finally:
        generator.engine.dispose()

    generator = Generator(settings, model=Model(responses=["SELECT id FROM anchor"]))
    try:
        import sqlglot
        from sqlglot import exp
        result = generator.generate("Anchor IDs")
        assert result["default_schema"] == "dbo"
        assert sqlglot.parse_one(result["sql"], read="tsql").find(exp.Table).db == "dbo"
    finally:
        generator.engine.dispose()

    from langchain_core.runnables import RunnableLambda
    from MCP.generator import ScopeDecision
    class MultiSchemaModel(Model):
        def with_structured_output(self, schema, **kwargs):
            if schema is ScopeDecision:
                return RunnableLambda(lambda _: ScopeDecision(outcome="selected", names=["erp", "archive"]))
            return super().with_structured_output(schema, **kwargs)
    sql = "SELECT o.id, v.amount FROM erp.orders o JOIN archive.order_report v ON v.id = o.id"
    generator = Generator(settings, model=MultiSchemaModel(responses=[sql]))
    try:
        result = generator.generate("Compare current orders and archived order reports, matching their order IDs")
        assert result["sql"] == sql and result["schemas"] == ["archive", "erp"]
    finally:
        generator.engine.dispose()

# Exercise the real MCP boundary, where errors are deliberately sanitized.
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def mcp_request(url, arguments):
    environment = dict(os.environ, DATABASE_URL=url)
    params = StdioServerParameters(command=sys.executable, args=["-m", "MCP.server"], env=environment, cwd="/app")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("generate_sql", arguments)
async def check_mcp():
    # ODBC on Linux accepts a match against either Server or HostnameInCertificate.
    # Use a genuinely mismatched Server alias, not a wrong alternate for localhost.
    for url in [variants[0].replace(encoded, "wrong-password"), variants[0].replace("erp_primary", "missing_db"), variants[0].replace("@localhost:", "@wrong.invalid:")]:
        result = await mcp_request(url, {"question": "Return order IDs"})
        assert result.is_error
        text = str(result.content)
        assert encoded not in text and "Reader-p" not in text and "mssql+" not in text
        assert "ODBC" not in text and "28000" not in text
    if os.getenv("RUN_LIVE_DEEPSEEK") == "1":
        import sqlglot
        from sqlglot import exp
        response = await mcp_request(variants[0], {"question": "Return customer ID and SUM(order amount * 2), joining customers to orders on customer_id = customers.id. Only amount > 10. Group by customer ID."})
        assert not response.is_error
        result = response.structured_content
        tree = sqlglot.parse_one(result["sql"], read="tsql")
        assert all(tree.find(kind) is not None for kind in [exp.Join, exp.Group, exp.Where, exp.Sum, exp.Mul])
        assert all(t.db.lower() == "erp" for t in tree.find_all(exp.Table))
        for question, expected in [
            ("For current records, what is the total order value for each customer?", "erp"),
            ("Which customers in the archived records have never placed an order?", "archive"),
        ]:
            response = await mcp_request(variants[0], {"question": question})
            assert not response.is_error
            assert response.structured_content["schema"] == expected
            assert response.structured_content["execution_performed"] is False

        assert result["execution_performed"] is False
asyncio.run(check_mcp())
print("SQL Server verified TLS, URL variants, metadata isolation, sanitized failures and generation checks passed")
"""


@pytest.mark.parametrize("version", ["2022", "2025"])
def test_sqlserver_usage(version):
    image = os.getenv("MCP_TEST_IMAGE")
    if not image:
        pytest.fail("SQL Server checks require MCP_TEST_IMAGE with ODBC Driver 18")
    root = Path(__file__).resolve().parents[1]
    name = "atlas-sqlserver-usage-" + uuid.uuid4().hex[:12]
    environment = os.environ.copy()
    values = dotenv_values(root / ".env")
    for key in ["DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"]:
        environment[key] = os.getenv(key) or values.get(key) or ""
    if os.getenv("RUN_LIVE_DEEPSEEK") == "1" and not environment["DEEPSEEK_API_KEY"]:
        pytest.fail("Live usage requires the configured provider key")
    environment["DEEPSEEK_API_KEY"] = environment["DEEPSEEK_API_KEY"] or "offline-test-only"
    environment["DEEPSEEK_MODEL"] = environment["DEEPSEEK_MODEL"] or "deepseek-flash"
    with tempfile.TemporaryDirectory(prefix="atlas-sqlserver-tls-") as directory:
        temporary = Path(directory)
        # Trust a dedicated CA, not the server leaf itself, so hostname checks
        # exercise a normal verified chain rather than direct leaf trust.
        commands = [
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(temporary / "ca.key"), "-out", str(temporary / "ca.crt"),
             "-days", "1", "-subj", "/CN=Disposable fixture CA",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(temporary / "server.key"), "-out", str(temporary / "server.csr"),
             "-subj", "/CN=localhost"],
        ]
        (temporary / "server.ext").write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        )
        commands.append([
            "openssl", "x509", "-req", "-in", str(temporary / "server.csr"),
            "-CA", str(temporary / "ca.crt"), "-CAkey", str(temporary / "ca.key"),
            "-CAcreateserial", "-out", str(temporary / "server.crt"), "-days", "1",
            "-extfile", str(temporary / "server.ext"),
        ])
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
                    "--env",
                    "DEEPSEEK_API_KEY",
                    "--env",
                    "DEEPSEEK_MODEL",
                    "--env",
                    "RUN_LIVE_DEEPSEEK",
                    "--entrypoint",
                    "sh",
                    image,
                    "-c",
                    "cp /fixtures/ca.crt /usr/local/share/ca-certificates/fixture.crt && update-ca-certificates >/dev/null 2>&1 && PYTHONPATH=/app python /fixtures/usage.py",
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=240,
            )
            # Raw driver/provider errors may contain secrets. Never put captured
            # output in assertions or pytest diagnostics.
            failure = result.returncode != 0
            if failure:
                # Expose only exception class and script line, never its message.
                import re

                frames = re.findall(r'File "/fixtures/usage.py", line (\d+)', result.stderr)
                category = re.findall(r"^([A-Za-z]+(?:Error|Exception)):", result.stderr, re.M)
                print(result.stdout)
                print(
                    "TLS failure:",
                    "certificate verify failed" in result.stderr.lower(),
                    "login failure:",
                    "Login failed" in result.stderr,
                )
                pytest.fail(
                    "Disposable SQL Server check failed at fixture line "
                    + (frames[-1] if frames else "startup")
                    + "; category "
                    + (category[-1] if category else "unknown")
                )
        finally:
            if started:
                subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)
