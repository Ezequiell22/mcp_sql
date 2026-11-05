"""Opt-in real MCP-in-Docker → verified-TLS SQL Server integration.

Creates its own network, certificates and disposable database. No customer DSN,
existing container, production records or TLS bypass enters the product path.
"""

import asyncio
import os
import secrets
import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import args_for, with_range
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOW_SYNTHETIC_DB_WRITES") != "1"
    or os.environ.get("ACCEPT_SQLSERVER_EULA") != "Y"
    or not os.environ.get("ATLAS_SQL_DOCKER_IMAGE"),
    reason="Requires Docker MCP image, isolated-write opt-in and SQL Server EULA acceptance",
)


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120, **kwargs)


def certificates(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "erp-sql")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("erp-sql")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    (folder / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "server.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    (folder / "server.key").chmod(0o644)  # Ephemeral fixture only; readable by container mssql.
    (folder / "mssql.conf").write_text(
        "[network]\ntlscert = /var/opt/mssql/server.pem\n"
        "tlskey = /var/opt/mssql/server.key\nforceencryption = 1\n"
    )


@pytest.fixture(scope="module")
def live_sqlserver(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sqlserver-tls")
    certificates(folder)
    suffix = secrets.token_hex(6)
    network, container = f"atlas-live-net-{suffix}", f"atlas-live-sql-{suffix}"
    admin_password = "Synthetic!" + secrets.token_hex(16)
    reader_password = "Reader!" + secrets.token_hex(16)
    image = os.environ["ATLAS_SQL_DOCKER_IMAGE"]
    docker("network", "create", "--internal", network, check=True)

    def execute(sql):
        return docker(
            "exec",
            "-i",
            "--env",
            f"SQLCMDPASSWORD={admin_password}",
            container,
            "/opt/mssql-tools18/bin/sqlcmd",
            "-S",
            "tcp:127.0.0.1,1433",
            "-U",
            "sa",
            "-C",
            "-l",
            "2",
            "-t",
            "5",
            "-b",
            "-m",
            "1",
            "-h",
            "-1",
            "-W",
            "-s",
            "|",
            input="SET NOCOUNT ON;\n" + sql,
        )

    try:
        docker(
            "create",
            "--name",
            container,
            "--network",
            network,
            "--network-alias",
            "erp-sql",
            "--env",
            "ACCEPT_EULA=Y",
            "--env",
            f"MSSQL_SA_PASSWORD={admin_password}",
            os.environ.get(
                "ATLAS_SQL_SQLSERVER_IMAGE", "mcr.microsoft.com/mssql/server:2022-latest"
            ),
            check=True,
        )
        for file in ("server.pem", "server.key", "mssql.conf"):
            docker("cp", str(folder / file), f"{container}:/var/opt/mssql/{file}", check=True)
        docker("start", container, check=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = execute("SELECT 1;")
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("Isolated SQL Server TLS fixture failed to start")
        version = execute("SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int);")
        assert version.returncode == 0, "Native SQL Server version could not be verified"
        database_major = int(version.stdout.strip())
        assert database_major in {16, 17}
        setup = execute(f"""
CREATE DATABASE erp;
GO
USE erp;
CREATE LOGIN erp_reader WITH PASSWORD='{reader_password}', CHECK_POLICY=OFF;
CREATE USER erp_reader FOR LOGIN erp_reader;
CREATE TABLE dbo.accounts(id int NOT NULL PRIMARY KEY, region nvarchar(40) NOT NULL);
CREATE TABLE dbo.invoices(id int NOT NULL PRIMARY KEY, account_id int NOT NULL
 REFERENCES dbo.accounts(id), amount decimal(20,2) NOT NULL, issued_on date NOT NULL);
INSERT dbo.accounts VALUES(1,N'North'),(2,N'South');
INSERT dbo.invoices VALUES(1,1,100,'2026-01-01'),(2,1,75,'2026-01-02'),(3,2,50,'2026-01-03');
GRANT SELECT ON dbo.accounts TO erp_reader;
GRANT SELECT ON dbo.invoices TO erp_reader;
""")
        assert setup.returncode == 0, "Isolated SQL Server fixture setup failed"
        yield {
            "folder": folder,
            "network": network,
            "image": image,
            "password": reader_password,
            "execute": execute,
            "database_major": database_major,
        }
    finally:
        docker("rm", "--force", container)
        docker("network", "rm", network)


def parameters(fixture, *, trusted=True):
    args = [
        "run",
        "--rm",
        "-i",
        "--network",
        fixture["network"],
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,size=128m",
    ]
    if trusted:
        args += [
            "--mount",
            f"type=bind,source={fixture['folder'] / 'server.pem'},target=/test-ca.pem,readonly",
            "--env",
            "SSL_CERT_FILE=/test-ca.pem",
        ]
    args.append(fixture["image"])
    return StdioServerParameters(command="docker", args=args)


@pytest.mark.parametrize(
    "query,outputs,expected,date_filter",
    [
        (
            "SELECT a.region, SUM(i.amount) AS total FROM dbo.invoices i "
            "JOIN dbo.accounts a ON a.id=i.account_id GROUP BY a.region ORDER BY a.region",
            ["region", "total"],
            ["North|175.00", "South|50.00"],
            False,
        ),
        (
            "WITH balances AS (SELECT account_id, SUM(amount) AS total FROM dbo.invoices "
            "GROUP BY account_id) SELECT a.region, b.total FROM dbo.accounts a "
            "JOIN balances b ON b.account_id=a.id ORDER BY a.region",
            ["region", "total"],
            ["North|175.00", "South|50.00"],
            False,
        ),
        (
            "WITH ranked AS (SELECT id, amount, ROW_NUMBER() OVER(ORDER BY amount DESC, id) "
            "AS ranking FROM dbo.invoices) SELECT id, amount, ranking FROM ranked "
            "WHERE ranking <= 2 ORDER BY ranking",
            ["id", "amount", "ranking"],
            ["1|100.00|1", "2|75.00|2"],
            False,
        ),
        (
            "SELECT a.region, SUM(i.amount) AS total FROM dbo.invoices i "
            "JOIN dbo.accounts a ON a.id=i.account_id "
            "WHERE i.issued_on >= CAST(@p1 AS date) AND i.issued_on < CAST(@p2 AS date) "
            "GROUP BY a.region ORDER BY a.region",
            ["region", "total"],
            ["North|75.00", "South|50.00"],
            True,
        ),
    ],
    ids=["regional-totals", "preaggregated-balances", "invoice-ranking", "typed-date-filter"],
)
def test_docker_mcp_sqlserver_verified_tls_reporting(
    live_sqlserver, query, outputs, expected, date_filter
):
    async def run():
        async with stdio_client(parameters(live_sqlserver)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                connected = (
                    await session.call_tool(
                        "atlas_sql_connect",
                        {
                            "connectionString": "Server=erp-sql;Database=erp;UID=erp_reader;PWD="
                            + live_sqlserver["password"],
                        },
                    )
                ).structured_content
                assert connected["status"] == "connected", connected
                context = (
                    await session.call_tool(
                        "atlas_sql_context_get",
                        {
                            "contractVersion": "1.2",
                            "connectionId": connected["connectionId"],
                            "request": "Invoice totals by region",
                        },
                    )
                ).structured_content
                assert context["status"] == "context", context
                assert context["databaseMajorVersion"] == live_sqlserver["database_major"]
                proposal = args_for(query, outputs, context["contextId"])
                if date_filter:
                    proposal = with_range(proposal)
                    for parameter in proposal["parameters"]:
                        parameter["sqlType"] = parameter.pop("postgresType")
                proposal["contractVersion"] = "1.2"
                result = (
                    await session.call_tool("atlas_sql_finalize", proposal)
                ).structured_content
                assert result["status"] == "validated", result
                assert result["artifact"]["sql"] == query
                assert result["artifact"]["validation"]["executionPerformed"] is False
                return query

    query = asyncio.run(run())
    # Independent test harness executes data reads; MCP product never executes this SQL.
    query = query.replace("@p1", "'2026-01-02'").replace("@p2", "'2026-01-04'")
    result = live_sqlserver["execute"]("USE erp;\n" + query + ";")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == expected


def test_docker_mcp_rejects_untrusted_sqlserver_certificate(live_sqlserver):
    async def run():
        async with stdio_client(parameters(live_sqlserver, trusted=False)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = (
                    await session.call_tool(
                        "atlas_sql_connect",
                        {
                            "connectionString": "Server=erp-sql;Database=erp;UID=erp_reader;PWD="
                            + live_sqlserver["password"],
                        },
                    )
                ).structured_content
                assert result["status"] == "error", result
                assert result["error"]["code"] == "CONNECTION_UNAVAILABLE", result
                assert live_sqlserver["password"] not in str(result)

    asyncio.run(run())


def test_docker_mcp_sqlserver_rejects_unsafe_agent_proposals(live_sqlserver):
    async def run():
        async with stdio_client(parameters(live_sqlserver)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                invalid = (
                    await session.call_tool(
                        "atlas_sql_connect",
                        {
                            "connectionString": "Server=erp-sql;Database=erp;UID=erp_reader;PWD=invalid",
                        },
                    )
                ).structured_content
                assert invalid["error"]["code"] == "CONNECTION_UNAVAILABLE", invalid
                connected = (
                    await session.call_tool(
                        "atlas_sql_connect",
                        {
                            "connectionString": "Server=erp-sql;Database=erp;UID=erp_reader;PWD="
                            + live_sqlserver["password"],
                        },
                    )
                ).structured_content
                assert connected["status"] == "connected", connected
                context = (
                    await session.call_tool(
                        "atlas_sql_context_get",
                        {
                            "contractVersion": "1.2",
                            "connectionId": connected["connectionId"],
                            "request": "List invoice IDs",
                        },
                    )
                ).structured_content
                assert context["status"] == "context", context
                for query in (
                    "DELETE FROM dbo.invoices",
                    "SELECT invented_column AS id FROM dbo.invoices",
                    "SELECT id FROM dbo.invoices; SELECT id FROM dbo.invoices",
                    "SELECT object_id AS id FROM sys.tables",
                ):
                    proposal = args_for(query, ["id"], context["contextId"])
                    proposal["contractVersion"] = "1.2"
                    result = (
                        await session.call_tool("atlas_sql_finalize", proposal)
                    ).structured_content
                    assert result["status"] == "error", result
                    assert "artifact" not in result
                    assert live_sqlserver["password"] not in str(result)

    asyncio.run(run())
