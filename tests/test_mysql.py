"""MySQL integration uses a NEW disposable TLS database, never a customer DSN."""

import os
import subprocess
import time
import uuid

import pymysql
import pytest
from sqlalchemy import event
from sqlalchemy.engine import URL

from MCP.generator import Generator
from MCP.settings import Settings
from tests.test_mcp import Model

pytestmark = pytest.mark.skipif(os.getenv("ALLOW_SYNTHETIC_DB_WRITES") != "1", reason="Disposable database gate is not enabled")


@pytest.fixture(scope="module")
def mysql(tmp_path_factory):
    directory = tmp_path_factory.mktemp("mysql-tls")
    directory.chmod(0o755)
    commands = [
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(directory / "ca.key"), "-out", str(directory / "ca.crt"), "-days", "1", "-subj", "/CN=Disposable MySQL CA", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash", "-addext", "authorityKeyIdentifier=keyid:always"],
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(directory / "server.key"), "-out", str(directory / "server.csr"), "-subj", "/CN=localhost"],
    ]
    (directory / "server.ext").write_text("basicConstraints=critical,CA:FALSE\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
    commands.append(["openssl", "x509", "-req", "-in", str(directory / "server.csr"), "-CA", str(directory / "ca.crt"), "-CAkey", str(directory / "ca.key"), "-CAcreateserial", "-out", str(directory / "server.crt"), "-days", "1", "-extfile", str(directory / "server.ext")])
    for command in commands:
        subprocess.run(command, check=True, capture_output=True)
    (directory / "server.key").chmod(0o644)  # Disposable key only.
    name = "atlas-mysql-test-" + uuid.uuid4().hex[:12]
    subprocess.run(["docker", "run", "--rm", "-d", "--name", name, "--memory", "1g", "-e", "MYSQL_ROOT_PASSWORD=disposable-admin-only", "-e", "MYSQL_ROOT_HOST=%", "-p", "127.0.0.1::3306", "-v", f"{directory}:/tls:ro", "mysql:8.4", "--ssl-ca=/tls/ca.crt", "--ssl-cert=/tls/server.crt", "--ssl-key=/tls/server.key", "--require-secure-transport=ON", "--performance-schema=OFF"], check=True, capture_output=True)
    try:
        port = int(subprocess.check_output(["docker", "port", name, "3306/tcp"], text=True).strip().rsplit(":", 1)[1])
        options = dict(host="127.0.0.1", port=port, user="root", password="disposable-admin-only", ssl_ca=str(directory / "ca.crt"), ssl_verify_cert=True, ssl_verify_identity=True, connect_timeout=1, read_timeout=2)
        deadline = time.monotonic() + 90
        while True:
            try:
                admin = pymysql.connect(**options, autocommit=True)
                break
            except pymysql.MySQLError:
                if time.monotonic() >= deadline:
                    pytest.fail("Disposable MySQL TLS startup failed; dependency details suppressed")
                time.sleep(0.25)
        with admin:
            with admin.cursor() as cursor:
                cursor.execute("CREATE USER 'reader'@'%' IDENTIFIED BY 'disposable-reader-only' REQUIRE SSL")
                for scope in ["erp", "archive"]:
                    cursor.execute(f"CREATE DATABASE {scope}")
                    cursor.execute(f"CREATE TABLE {scope}.orders(id integer PRIMARY KEY, amount decimal(18,2))")
                    cursor.execute(f"CREATE VIEW {scope}.order_report AS SELECT id, amount FROM {scope}.orders")
                    cursor.execute(f"GRANT SELECT, SHOW VIEW ON {scope}.* TO 'reader'@'%'")
        yield URL.create("mysql+pymysql", username="reader", password="disposable-reader-only", host="127.0.0.1", port=port, database="erp", query={"ssl_ca": str(directory / "ca.crt")}).render_as_string(hide_password=False)
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)


def test_mysql_tls_metadata_views_cross_schema_and_readonly_session(mysql, monkeypatch):
    from tests.test_production import MultiSchemaModel
    monkeypatch.setattr("MCP.settings.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DATABASE_URL", mysql)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-only")
    settings = Settings.load()
    sql = "SELECT a.id, r.amount FROM erp.orders a JOIN archive.order_report r ON a.id = r.id"
    generator = Generator(settings, model=MultiSchemaModel(responses=[sql]))
    statements = []
    event.listen(generator.engine, "before_cursor_execute", lambda c, cur, s, p, ctx, many: statements.append(s))
    try:
        result = generator.generate("Compare current orders and archived reports by matching order IDs")
        assert result["sql"] == sql and result["dialect"] == "mysql"
        assert result["schemas"] == ["archive", "erp"]
        assert not result["execution_performed"]
        assert not any("from erp.orders" in statement.lower() or "from archive.order_report" in statement.lower() for statement in statements)
        with generator.engine.connect() as connection:
            # Session configuration only, never application rows/generated SQL.
            assert connection.exec_driver_sql("SELECT @@session.transaction_read_only").scalar() == 1
            assert connection.exec_driver_sql("SHOW SESSION STATUS LIKE 'Ssl_cipher'").one()[1]
        generator.model = Model(responses=["SELECT id FROM orders"])
        result = generator.generate("Current order IDs")
        import sqlglot
        from sqlglot import exp
        assert result["default_schema"] == "erp"
        assert sqlglot.parse_one(result["sql"], read="mysql").find(exp.Table).db == "erp"
    finally:
        generator.engine.dispose()


def test_mysql_tls_hostname_mismatch_is_rejected(mysql, monkeypatch):
    import socket
    from sqlalchemy.engine import make_url
    url = make_url(mysql).set(host="wrong.invalid")
    getaddrinfo = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: getaddrinfo("127.0.0.1" if host == "wrong.invalid" else host, *a, **k))
    generator = Generator(Settings(url.render_as_string(hide_password=False), "offline"), model=Model(responses=["unused"]))
    try:
        from sqlalchemy.exc import OperationalError
        with pytest.raises(OperationalError):
            generator.generate("Order IDs")
    finally:
        generator.engine.dispose()


def test_live_mysql_deepseek_over_stdio(mysql):
    if os.getenv("RUN_LIVE_DEEPSEEK") != "1":
        pytest.skip("Enable billed provider checks with RUN_LIVE_DEEPSEEK=1")
    import asyncio
    import sys
    from pathlib import Path
    from dotenv import dotenv_values
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / ".env")
    key = os.getenv("DEEPSEEK_API_KEY") or values.get("DEEPSEEK_API_KEY")
    if not key:
        pytest.fail("Live test needs the configured provider key; credentials are never printed")
    params = StdioServerParameters(command=sys.executable, args=["-m", "MCP.server"], cwd=str(root), env={
        "DATABASE_URL": mysql, "DEEPSEEK_API_KEY": key,
        "DEEPSEEK_MODEL": os.getenv("DEEPSEEK_MODEL") or values.get("DEEPSEEK_MODEL") or "deepseek-flash",
    })

    async def check():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool("generate_sql", {"question": "Compare current orders against archived order reports. Join current orders to archived order reports by matching their id columns. Return current order id and archived report amount, without any other filters."})
                assert not response.is_error, "Live MySQL generation failed; protected output suppressed"
                result = response.structured_content
                assert result["status"] == "generated" and result["dialect"] == "mysql"
                assert result["schemas"] == ["archive", "erp"] and not result["execution_performed"]
                assert "erp.orders" in result["tables"] and "archive.order_report" in result["tables"]
                assert key not in result["sql"]
                # Never execute the generated query.

    asyncio.run(check())
