"""Optional native T-SQL conformance in a newly created disposable container.

No host port, mounts, existing container or customer connection can be supplied.
The certificate bypass applies only to sqlcmd inside this disposable fixture;
the product adapter always verifies TLS certificates.
"""

import os
import secrets
import shutil
import subprocess
import time

import pytest


@pytest.fixture(scope="module")
def sqlserver_engine():
    if (
        os.environ.get("ALLOW_SYNTHETIC_DB_WRITES") != "1"
        or os.environ.get("ACCEPT_SQLSERVER_EULA") != "Y"
    ):
        pytest.skip(
            "Native SQL Server fixture requires synthetic-write opt-in and EULA acceptance."
        )
    if not shutil.which("docker"):
        pytest.fail("Docker is required for explicitly requested SQL Server conformance.")
    name = "atlas-sql-conformance-" + secrets.token_hex(8)
    password = "Synthetic!" + secrets.token_hex(20)
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "none",
            "--env",
            "ACCEPT_EULA=Y",
            "--env",
            f"MSSQL_SA_PASSWORD={password}",
            "mcr.microsoft.com/mssql/server:2022-latest",
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )

    def execute(sql):
        return subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "--env",
                f"SQLCMDPASSWORD={password}",
                name,
                "/opt/mssql-tools18/bin/sqlcmd",
                "-S",
                "localhost",
                "-U",
                "sa",
                "-C",
                "-b",
                "-h",
                "-1",
                "-W",
                "-s",
                "|",
            ],
            input="SET NOCOUNT ON;\n" + sql,
            text=True,
            capture_output=True,
            timeout=10,
        )

    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            probe = execute("SELECT 1;\n")
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("Disposable SQL Server did not become ready.")
        yield execute
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name], capture_output=True, timeout=30, check=True
        )


@pytest.mark.parametrize(
    "expression,kind,precision,scale,value",
    [
        ("SUM(CAST(v AS smallint))", "int", "10", "0", "6"),
        ("AVG(CAST(v AS smallint))", "int", "10", "0", "2"),
        ("SUM(CAST(v AS decimal(20,2)))", "decimal", "38", "2", "6.00"),
        ("AVG(CAST(v AS decimal(20,2)))", "decimal", "38", "6", "2.000000"),
        ("COUNT(*)", "int", "10", "0", "3"),
        ("COUNT_BIG(*)", "bigint", "19", "0", "3"),
    ],
)
def test_native_aggregate_types_and_values(
    sqlserver_engine, expression, kind, precision, scale, value
):
    from atlas_sql_mcp.domain import Catalog, Column, Relation
    from atlas_sql_mcp.sqlserver import SqlServerAnalyzer

    catalog = Catalog(
        "fixture",
        0,
        (Relation("dbo", "fixture", (Column("v", "int", False, type_schema="sys"),)),),
        "test",
        "sqlserver",
        16,
    )
    derived = SqlServerAnalyzer().analyze(f"SELECT {expression} AS x FROM dbo.fixture", catalog)
    expected = f"decimal({precision},{scale})" if kind == "decimal" else kind
    assert derived["outputs"][0]["sqlType"] == expected
    result = sqlserver_engine(f"""
WITH result AS (
 SELECT {expression} AS x FROM (VALUES(1),(2),(3)) fixture(v)
)
SELECT SQL_VARIANT_PROPERTY(x,'BaseType'), SQL_VARIANT_PROPERTY(x,'Precision'),
       SQL_VARIANT_PROPERTY(x,'Scale'), x FROM result;
""")
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert rows == ["|".join((kind, precision, scale, value))]
