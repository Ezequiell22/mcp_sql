# Atlas SQL MCP

A small stdio MCP server that translates natural language into PostgreSQL, SQL
Server or MySQL SELECT queries using **LangChain + DeepSeek on the server**. It supports
joins, grouping, filters, calculated expressions, CTEs and windows through model
generation. It returns SQL, never query results. It does not execute generated
SQL, read business rows, run EXPLAIN/PREPARE or create database objects.

## Structure

```
MCP/settings.py       environment loading and connection policy
MCP/generator.py      LangChain selection, SQL generation and structural guard
MCP/server.py         one MCP tool over stdio
MCP/prompts.toml      MCP, metadata-selection and SQL-generation prompts
MCP/requirements.txt  pinned dependencies
specs/               documentation
tests/               regression and integration checks
AGENTS.md
.env              private, ignored, never copied into the image
Dockerfile
compose.yaml
```

This intentionally replaces the old connect/context/finalize service. One
connection and reader account are configured per server instance. Schema and tables are
chosen independently on every request. This Docker setup runs one persistent MCP container. Change the configured
database/reader and restart when the deployment scope changes. The client does not
supply credentials and does not need to generate SQL itself.

## Configuration

Use the single .env in the repository root. Existing environment variables take precedence. Restart after
changing configuration. Never commit this file or put passwords in MCP prompts.

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
DATABASE_URL=
```

Set DATABASE_URL to a SQLAlchemy URL, percent-encoding special characters in the
username/password. PostgreSQL example (replace all placeholders):

```
postgresql+psycopg://reader:PASSWORD@db.example.com:5432/erp?sslmode=verify-full
```

SQL Server example:

```
mssql+pyodbc://reader:PASSWORD@db.example.com:1433/erp?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no
```

Use an existing restricted database account: only metadata visibility and SELECT
on intended tables; no owner/admin, DDL, DML or routine execution permissions.
The application does not provision accounts or establish row-level authorization.
Schema and table selection happen automatically for each business question.
The sole public input is question; schema and table names are never public inputs. No request scope is fixed in environment configuration.
Tables and views in up to eight selected schemas per request are supported;
consecutive requests may use different schemas without restarting. MySQL schemas
are databases on the same server. SQL Server cross-database references and
connections to multiple servers in one query are not supported. PostgreSQL
materialized views are included when supported by the inspector. There is no database-version gate;
SQLAlchemy/driver compatibility must be verified for the destination version.

Remote PostgreSQL requires certificate-verified TLS; loopback may omit TLS.
The host belongs in the URL authority; query-string host/hostaddr overrides and
embedded ODBC connection strings are rejected so they cannot bypass TLS policy.
SQL Server uses ODBC Driver 18 with encryption and certificate verification.
MySQL uses PyMySQL with a certificate- and hostname-verifying SSLContext; use
`mysql+pymysql://reader:PASSWORD@db.example.com:3306/erp?ssl_ca=/path/to/ca.crt`
for a private CA. Only `ssl_ca` and `charset` query options are accepted for MySQL.
Its reader also needs SHOW VIEW to reflect views. PostgreSQL preserves the connection's native default schema and uses read-only sessions; MySQL uses read-only sessions. SQL Server
requires privileges restricted by the administrator; connection intent is not
write authorization.

Install the organization's trusted CA in the host/image when needed. Docker
localhost is the container itself; use a reachable database hostname. Do not enable
LangSmith tracing or SDK debug logging with sensitive schema/request data.

## Run locally

Python 3.12 or later. SQL Server also requires Microsoft ODBC Driver 18.
From the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r MCP/requirements.txt
.venv/bin/python -m MCP.server
```

Configure the MCP client to launch that Python executable with arguments
`["-m", "MCP.server"]` and this repository as its working directory. .env is
loaded by its absolute project location. Stdio is the only transport; there is no
unauthenticated HTTP endpoint.

## Run with Docker Compose

The image includes both drivers. Building requires the operator to accept
Microsoft's ODBC driver EULA; the default build refuses without acceptance:

```sh
ACCEPT_EULA=Y docker compose build
docker compose up -d --wait
```

Compose maintains one container, `atlas-sql-mcp`, with one persistent Python MCP listener. Each valid request starts a fresh,
terminable worker process and removes it on completion, failure or timeout.
The server uses private local stdio pipes so MCP protocol responses are separated
from Docker diagnostics. A short bridge connects the client's stdin/stdout to
that same server; it does not start another MCP process. A file lock permits one
client at a time. Disconnect Inspector before connecting another client.

Use the server entry in the ignored local `.env.inspector/mcp.json` with Inspector.
For other clients, copy that entry's command/args. It uses `docker exec -i` with
`flock` and stream forwarding; do not use `docker compose run` or start Python with
`docker exec`, which would create another listener. The runtime is still stdio-only;
there is no MCP HTTP endpoint.

The single root `.env` supplies both runtime credentials and Compose settings.
`DATABASE_DOCKER_NETWORK` names the existing database network;
`DATABASE_CA_FILE` points to its CA certificate on the host. Compose mounts this
certificate read-only at `/run/atlas/database-ca.crt`; `PGSSLROOTCERT` uses that
container path. The local deployment targets the existing iDempiere ERP database,
without provisioning or inserting business data. There are no alternate env files
or fixed schema/table selectors. Existing private connection files are preserved.

Secrets enter through env_file, never build arguments. Protect the Docker host:
operators with Docker access can inspect container environments.

## Docker execution logs

```sh
docker compose logs -f mcp
```

In Docker Desktop, select `atlas-sql-mcp` and open Logs. JSON events cover startup,
readiness, each request, metadata discovery, source/table/column selection,
reflection/context construction, model generation, SQL validation, completion and
failure. Request IDs correlate stages; durations, counts, application-owned error
reasons, error types and available HTTP status/SQLSTATE codes help diagnosis.

Questions, SQL text, model prompts, schema/table names, connection URLs,
credentials and raw provider/driver exceptions are excluded. Protocol stdout uses
a private pipe, so enabling Docker logs does not copy initialization instructions
or generated SQL into them. Logs rotate at 10 MB with three files retained.
Startup checks report whether database/provider configuration exists, without
revealing its values. No business rows or generated SQL are executed for logging.

## Tool contract

`generate_sql(question: str)`

Question: 1–8000 UTF-8 bytes. Discover nonempty non-system schemas from metadata
and select the matching schemas on every request. Empty default
schemas do not prevent discovery. Ambiguous sources require business-level
clarification, never physical identifiers. Include metric definitions, date
meanings, company/branch restrictions and literal filter values in the question.
For a ranking such as "top 10 customers", explain the ranking measure, for example
total order value, and the period to consider.

For example, the same server can receive these requests consecutively:

```json
{"question": "What is the total order value for each customer?"}
{"question": "How many archived orders are there?"}
```

LangChain selects the data scope and relevant tables automatically. Metadata is reconstructed per request; inspection of one schema never
authorizes a reference to it in another request that did not select that schema. Every physical table
in returned SQL must be schema-qualified to match the inspected request metadata; CTE aliases
are exempt. Existing explicit schemas are never changed. An omitted physical-table
schema is filled only with the connection's verified default, and only when that
object exists in this request's selected metadata. CTE aliases are not qualified.
The result exposes `default_schema`; defaults commonly are public, dbo, or the
configured MySQL database, but reader accounts can override them.

Successful result:

```json
{
  "status": "generated",
  "sql": "SELECT region, SUM(amount) AS revenue FROM public.sales GROUP BY region",
  "dialect": "postgresql",
  "schema": "public",
  "schemas": ["public"],
  "tables": ["public.sales"],
  "execution_performed": false,
  "warnings": ["Business meaning, database execution and query cost are not verified."]
}
```

`default_schema` reports the connection default used for omitted physical
qualifiers. `schemas` lists schemas represented in the selected metadata. `schema` retains
the single-schema value for compatibility and is null for multi-schema scope.
`tables` identifies context tables/views, potentially including tables unused by the
final query. There are no sessions, cursors or finalize calls. Errors are MCP
tool errors for infrastructure or invalid output; credentials and raw provider/driver messages are hidden.
Business ambiguity returns status=needs_clarification with a safe clarification
message and execution_performed=false, without SQL or an MCP Tool Error. A client
must resubmit the complete business question including the clarification. No
conversation, prior question, selected scope or catalog is cached across calls.

## Large databases

The flow follows LangChain's large-database guide: select relevant tables first,
then supply their schema to create_sql_query_chain. The calling agent receives
the SQL, not an exploration history.

- Automatic schema discovery considers up to 100 nonempty schemas and 10000
  tables/views in total; multi-schema selection context is limited to 64000 bytes.
  System and empty schemas are excluded.
- Table/view names are selected internally in batches of 250; there is no fixed
  maximum number of selected tables and no user-supplied table list. Metadata
  byte and request-time budgets bound how much context can be used. Up to 10000 tables for automatic selection; larger catalogs must be narrowed through database permissions.
- Wide tables get model selection of up to 60 columns plus primary/join keys,
  with a final limit of 80 columns per table.
- SQLAlchemy reflects selected tables/columns without recursively loading FK
  targets. Relationships outside the selected request metadata are omitted; selected
  cross-schema foreign keys are retained.
- LangChain uses lazy reflection with sample rows disabled. No distinct-value
  lookup, business-row samples, vector database or persistent catalog is required.
- Schema context: 64000 UTF-8 bytes; SQL: 64000 bytes. Oversized metadata is
  rejected, never silently truncated. No automatic result-row limit.
- One generation at a time per process. Provider calls: 30-second timeout,
  no automatic retries, 8192 output tokens. A parent-enforced 120-second deadline includes worker startup and in-flight
  operations. Overdue workers are terminated and, if necessary, killed and reaped
  with up to two seconds of bounded cleanup. Database access also has driver
  timeouts. Overlapping requests fail immediately as busy; there is no request
  queue. Scale with independent server instances when needed.

Batching does not guarantee semantic retrieval quality: 3000 tables can require
12 selection calls plus reduction/generation. For opaque ERP names, include business explanations and configure meaningful metadata.
Wide-column selection sends column names to DeepSeek. Schema and request text
leave the server for that provider; connection URLs/passwords are not included.

The SQLGlot guard rejects non-SELECT statements, writes inside CTEs, SELECT INTO,
multiple statements, locks, unknown tables/columns, external table sources,
projection wildcards and unresolved parameters, table hints, sequence advancement and unclassified or
qualified function calls. Built-in read function families are explicitly allowed;
COUNT(*) is allowed. Parser errors fail closed. Unknown reflected column types
are rejected rather than silently omitted. It does not
replace the database compiler, authorize later execution, prove business meaning,
assess cost or certify all SELECT functions as harmless. Any execution consumer
needs its own authorization and controls.

## Prompts

Edit [MCP/prompts.toml](../MCP/prompts.toml), then restart the process (rebuild the
Docker image for container deployments). The file is loaded relative to the code,
so the client working directory does not change which prompt is used.

- mcp: instructions advertised to the calling agent: preserve business intent,
  accept ordinary business questions, discover scope per request, clarify business
  ambiguity, never ask users for database identifiers or claim execution.
- schema_selection: choose up to eight required business data scopes from catalog metadata.
- selection: bounded JSON metadata selection including filter/group/join keys.
  selection_repair supplies one corrective attempt when selected names violate
  the current batch or count limit; identifiers remain validated.
- sql: LangChain system prompt, separated from the human request. Covers output
  grain, preaggregation against fanout, composite keys, unmatched entities, NULLs,
  ratios, dates and native syntax for the selected dialect. Essential missing rules
  produce a clear refusal instead of invented SQL. No automatic row limit.

Keep LangChain placeholders dialect, table_info and top_k in the SQL section;
input is bound to a separate human message. The selection section uses limit and
purpose; literal JSON braces there are doubled for Python formatting. Prompts
cannot enforce security by themselves: SELECT/schema guards remain in Python.

## Verification and sources

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests
git diff --check
```

See [verification.md](verification.md) for actual results and limits. Integration
creates NEW disposable containers from locally available PostgreSQL 17/18, MySQL 8.4 and SQL
Server 2022/2025 images; existing containers and customer databases are never used.
SQL Server test runners use the MCP image's ODBC driver because the host may not
have it installed. Tests provision their own temporary certificate and verify TLS.

Full container/URL tests, without billed model calls:

```sh
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y MCP_TEST_IMAGE=atlas-sql-mcp:local .venv/bin/python -m pytest tests
```

Add RUN_LIVE_DEEPSEEK=1 to test the configured model through the actual MCP:

```sh
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y RUN_LIVE_DEEPSEEK=1 MCP_TEST_IMAGE=atlas-sql-mcp:local .venv/bin/python -m pytest tests
```

The live test reads only the provider key/model from .env, never uses a customer
DATABASE_URL, and never executes generated SQL. It covers automatic selection,
schema switching, joins/grouping/calculations/filters, wide-column selection and
Docker generation. URL cases include host/IP, query-option encodings, credentials
with reserved characters, database changes and sanitized connection/TLS failures.

- [SQL generation API](https://reference.langchain.com/python/langchain-classic/chains/sql_database/query/create_sql_query_chain)
- [Official large-database guide, v0.3](https://github.com/langchain-ai/langchain/blob/v0.3/docs/docs/how_to/sql_large_db.ipynb)
- [SQLDatabase source](https://github.com/langchain-ai/langchain-community/blob/main/libs/community/langchain_community/utilities/sql_database.py)
- [LangChain DeepSeek integration](https://docs.langchain.com/oss/python/integrations/chat/deepseek)
- [DeepSeek JSON output](https://api-docs.deepseek.com/guides/json_mode/)
- [Current DeepSeek models](https://api-docs.deepseek.com/quick_start/pricing/)
- [Thinking configuration](https://api-docs.deepseek.com/guides/thinking_mode/)
- [SQLAlchemy reflection](https://docs.sqlalchemy.org/en/20/core/reflection.html)

SQL generation currently lives in langchain-classic and SQLDatabase in
langchain-community, which emits an upstream sunset/deprecation notice. These
are the requested library components; monitor maintenance and migration guidance.
The historical guide is adapted to current packages, not copied unchanged.

## Manual Inspector testing

See [inspector.md](inspector.md) for the existing ERP connection, instructions and two requests.

See [review.md](review.md) for historical request-flow findings and
[production.md](production.md) for the current deployment contract and limits.

The container healthcheck checks a fresh event-loop heartbeat and live listener
PID, rather than FIFO existence. It does not certify database/provider availability
or query semantics. Health probes use no business rows or provider calls.
