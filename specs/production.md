# Production deployment contract

## Supported compatibility

The same server code supports PostgreSQL (`postgresql+psycopg`), SQL Server
(`mssql+pyodbc`, ODBC Driver 18) and MySQL (`mysql+pymysql`). Choose the connection
through DATABASE_URL and restart. No business schema, table list, customer data,
model response or SQL template is hardcoded. SQLite is a local regression fixture,
not an accepted production connection. Other database families are rejected until
their driver, TLS policy, reflection and SQL parser are implemented and verified.
A SQLAlchemy-compatible URL alone does not establish compatibility.

Tables, views and joins across up to eight selected schemas on that connection
are supported. PostgreSQL materialized views are discoverable. SQL Server
cross-database references and multi-server queries are rejected. MySQL databases
on the same server are represented as schemas. See verification.md for the actual
versions and checks run, not an assumed certification of every release.

The output contract is one SELECT, including CTEs, joins, subqueries, aggregation,
windows and SELECT set operations. Commands such as SHOW, DESCRIBE, EXPLAIN,
procedures and administrative statements are not accepted generated output.
The server never executes generated SQL. No implementation can promise every
read-only statement on every database family while also verifying it with a
finite structural guard and no database compilation/execution.

## Request and failure behavior

The sole public tool remains `generate_sql(question)`. Send the complete business
question on every call. It discovers real metadata afresh and keeps no business
conversation, catalog, table selection or SQL result between calls. Source choice
can select multiple schemas; every physical table reference must be qualified
and present in the current selected metadata. Missing physical-table qualifiers
are filled only with the actual connection default, verified against selected
metadata. Common defaults are public for PostgreSQL, dbo for SQL Server, and the
configured database for MySQL; account-specific defaults take precedence. No
existing explicit qualifier is changed, and CTE aliases are never qualified. Single-schema results preserve
`schema`; multi-schema results set it to null. `default_schema` exposes the
connection default used by qualification. `schemas` and `tables` describe the
metadata selected for this request, not necessarily every source used in SQL.

One persistent stdio listener admits one generation at a time. Each production
request runs in a new spawned worker with its own engine, DeepSeek client and
metadata. Its parent imposes a 120-second deadline including startup and in-flight
calls, followed by up to two seconds of bounded termination/kill/reaping cleanup.
Overlapping requests receive a sanitized busy error immediately. There is no
unbounded request queue or background continuation after a timed-out worker.
Process startup overhead is intentional; test throughput for the intended use.

Provider calls also have 30-second timeouts and no network retries. Selection
repair is limited to one attempt. Metadata and output budgets remain explicit:
100 nonempty schemas, 10,000 discovered objects, eight selected schemas,
80 columns per object, and 64 KB selection/context/SQL budgets. Oversized or
unsupported metadata fails explicitly. Unknown reflected column types are not
silently dropped. There is no fixed selected-table count limit and no schema/table-list argument
at either the public tool or generator entry point. A model can request business clarification instead of inventing
missing measures, dates or joins.

## Read-only boundary and privacy

Parser errors fail closed. The guard rejects writes, multiple statements, locks,
table/query hints, executable MySQL comments, SELECT INTO, uninspected/unknown identifiers, unresolved parameters,
projection wildcards, sequence advancement, qualified functions and functions
outside explicit read-function families. COUNT(*) remains supported. This is a
small structural policy, not proof of business meaning, successful compilation,
execution cost or harmless behavior under arbitrary database configuration.

Use existing restricted readers with SELECT and necessary metadata visibility;
no owner/admin, DDL/DML, routine execution or bypass-RLS privileges. MySQL view
reflection additionally requires SHOW VIEW. Views and custom database types can
have their own underlying behavior, so inspect their grants and definitions in the
destination environment. Any downstream executor needs its own authorization,
read-only transaction controls, trusted function search paths, restricted routine/type
privileges and execution/resource limits.

PostgreSQL metadata sessions use read-only transactions, a statement timeout and
the reader's configured native default schema. SQL Server depends on restricted database grants;
a connection routing hint would not enforce read-only privileges. MySQL metadata
sessions use read-only transactions, disable local file loading and use a verified
SSLContext. See [PyMySQL connection options](https://pymysql.readthedocs.io/en/latest/modules/connections.html)
for the driver's TLS controls. Certificate/hostname verification is required for
MySQL and SQL Server, and for remote PostgreSQL. No generated SQL is run to test
these controls.

LangSmith tracing is disabled by the server before dotenv/model setup. Application
logs omit questions, SQL, prompts, identifiers, URLs, credentials and raw dependency
errors. Reflection warnings are suppressed in the production entry point because
they can include identifiers. Stdio protocol replies stay in private pipes in
Docker and never share the Docker diagnostic stream. Temporary worker IPC is
request-local and is closed after each call.

## Deployment and health

Use the existing Docker build and private connection configuration documented in
README.md. The image contains exactly three production Python files and external
prompts; it includes the three production drivers. Accept Microsoft's driver EULA
before building. Never copy .env or connection files into the image.

The reviewed candidate is built locally as `atlas-sql-mcp:production-review`.
It is not automatically deployed or substituted for a running image. Deploy a
reviewed immutable image in the target environment and keep the previous image
available for rollback. Changing DATABASE_URL or prompts requires restart/rebuild
as documented. The current Compose service restarts after process failure and
runs without root, with a read-only filesystem and bounded resources.

`python -m MCP.server --healthcheck` checks a fresh heartbeat from the listener's
event loop and the listener PID. A stopped or blocked event loop becomes unhealthy
even if the protocol FIFO still exists. It does not consume the exclusive stdio
client, execute SQL or make billed provider calls. Health does not certify the
database/provider connection. Docker marks an unhealthy service for monitoring;
its restart policy handles process exit, not arbitrary unhealthy states.

## Release evidence

Use the actual results in verification.md. Isolated tests intentionally retain
mocks and SQLite. Database integration creates NEW disposable databases behind
ALLOW_SYNTHETIC_DB_WRITES=1; SQL Server also requires ACCEPT_SQLSERVER_EULA=Y.
RUN_LIVE_DEEPSEEK=1 enables billed real-provider checks against those disposable
databases only. No customer DATABASE_URL is used for fixture creation, and no
generated query is executed even in integration.

Before releasing to a particular environment, confirm its reader grants, trusted
CA, provider data handling requirements, network access and acceptable throughput.
The test matrix does not establish those environment-specific properties.
