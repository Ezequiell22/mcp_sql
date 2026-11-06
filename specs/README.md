# Atlas SQL MCP

A small stdio server with exactly one tool: `execute_select(sql: str)`.
It executes one SELECT against the connection fixed by `DATABASE_URL` and returns
all rows. PostgreSQL and SQL Server use SQLAlchemy with psycopg and pyodbc.
There is no LLM, SQL generation, schema discovery or connection argument.

## Configuration

Python 3.12+. SQL Server also needs Microsoft ODBC Driver 18.
Environment variables take precedence over the ignored root `.env`:

```dotenv
DATABASE_URL=postgresql+psycopg://reader:PASSWORD@db.example.com:5432/erp?sslmode=verify-full
QUERY_TIMEOUT_SECONDS=30
```

SQL Server URL:

```text
mssql+pyodbc://reader:PASSWORD@db.example.com:1433/erp?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no
```

Percent-encode credentials. Only these explicit driver names are supported.
Remote PostgreSQL requires `sslmode=verify-full`; SQL Server requires ODBC 18,
encryption and certificate verification. Install the organization's public CA when
needed. Embedded ODBC strings and PostgreSQL host/service overrides are rejected.
`QUERY_TIMEOUT_SECONDS` is an integer from 1 to 86400 (default 30). It sets the
connection timeout and PostgreSQL statement timeout or pyodbc query timeout.
These are database/driver deadlines, not a total MCP response deadline: fetching,
JSON conversion and delivery of a very large result can take longer. Driver/network
failures can also delay cancellation. No background query retry occurs.

Use an existing **read-only database user**: SELECT on intended tables/views only;
no DML, DDL, ownership/admin roles, database/server control or unsafe routine access.
The server does not create users or certify account permissions. SQL Server has no
PostgreSQL-style read-only transaction; restricted database grants are mandatory.
ApplicationIntent=ReadOnly alone is not a permission control. Administrators must
also review views, functions, operators, linked servers and default/public grants.

## Run

```sh
python3 -m venv .venv
.venv/bin/pip install -r MCP/requirements.txt
.venv/bin/python -m MCP.server
```

Point your MCP client's command at that Python executable, args `-m MCP.server`,
and working directory at this repository. The root `.env` is loaded by absolute
project location. Existing private files are preserved; old provider variables in
an existing private `.env` are ignored and can be removed by its owner.

## Contract and protections

```json
{"sql":"SELECT region, SUM(amount) AS revenue FROM public.sales GROUP BY region"}
```

```json
{"columns":["region","revenue"],"rows":[["South","123.45"]],"row_count":1}
```

Rows are arrays matching column order, so duplicate column names are preserved.
NULL is JSON null; decimals, UUIDs and intervals are strings; dates/times use ISO 8601;
binary values are objects with a `base64` string. Arrays/JSON are converted
recursively. Non-finite floats become strings. Unsupported values fail the whole
call rather than returning a partial result. An empty result retains columns and
has `rows: []`, `row_count: 0`.

SQLGlot checks one SELECT, including CTEs, unions, joins, grouping and windows.
It rejects writes (including inside CTEs), SELECT INTO, statement batches, locks,
hints, sequence advancement, placeholders, cross-database table references,
external table functions and unknown/qualified routines. Common built-in read
functions are allowed explicitly. Valid dialect syntax outside this subset fails
closed. The guard is structural, not a proof that views, functions, operators or
casts have no effects; permissions remain the security boundary.

SQL is sent unchanged, including any caller-supplied LIMIT/TOP/OFFSET. There is
no MAX_ROWS setting, injected limit, automatic pagination, result truncation,
metadata reflection, conversation state or result cursor. One query runs per
process; overlapping calls receive a busy error. Every call opens a fresh
connection, rolls back and closes it, including on failure. PostgreSQL additionally
uses read-only transactions. SQL Server uses restricted reader permissions.

**Large-result risk:** all rows are materialized in memory and serialized in one
MCP response. Large SELECTs can exhaust server/client memory, exceed client/context
limits, overload the database or expose more data than intended. Timeout does not
bound result size or JSON serialization. Choose appropriate columns, predicates,
aggregates or explicit limits in the submitted SQL. Failed delivery is not a
partial-results API. Do not send a query unless you accept its full result.

Errors are sanitized MCP Tool Errors. Raw SQL, rows, credentials and raw driver
errors are not logged by the application. Database-side audit logs and the MCP
client may retain SQL/results; protect those systems. There is no HTTP endpoint.

## Docker and existing deployment settings

The image includes both drivers and runs as an unprivileged user. The existing
Compose network, read-only filesystem, memory/CPU limits, persistent stdio bridge
and CA settings are retained. Building requires acceptance of Microsoft's EULA:

```sh
ACCEPT_EULA=Y docker compose build
# Replaces the running service; reconnect clients afterward.
docker compose up -d --wait
```

The ignored root `.env` also supplies `DATABASE_DOCKER_NETWORK` (existing network)
and `DATABASE_CA_FILE` (host path to a public CA). Compose mounts that CA read-only
and supplies it as a BuildKit secret; Docker installs it into the trust store for
SQL Server verification. PostgreSQL may use `PGSSLROOTCERT=/run/atlas/database-ca.crt`
in `.env`. Rebuild with `--no-cache` after CA rotation. Never supply private keys
or credentials as build arguments. Standalone builds may omit the optional CA
secret when using a publicly trusted database certificate.

The persistent listener uses private pipes under `/tmp/atlas-mcp` to keep protocol
responses out of Docker diagnostics. Clients must use the bridge and exclusive
lock described in [inspector.md](inspector.md). The healthcheck measures listener
liveness, not database availability or query success. No runtime deployment is
required to run the isolated tests, and tests do not alter an existing service.

## Verification

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y MCP_TEST_IMAGE=atlas-sql-mcp:select-test .venv/bin/python -m pytest tests
```

Integration uses only newly created disposable containers and synthetic reader
accounts. See [verification.md](verification.md) for actual evidence and limits.

Reference behavior: [SQLAlchemy driver execution](https://docs.sqlalchemy.org/en/21/core/connections.html#sqlalchemy.engine.Connection.exec_driver_sql),
[PostgreSQL statement timeout/read-only transactions](https://www.postgresql.org/docs/current/runtime-config-client.html),
[pyodbc query timeout](https://github.com/mkleehammer/pyodbc/wiki/Connection#timeout).
