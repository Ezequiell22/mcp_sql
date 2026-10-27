# Connect with a connection string

Call `atlas_sql_connect` with `connectionString`. No connection configuration file
or predeclared connection ID is required. The returned `connectionId` is used by
`atlas_sql_context_get` and, when enabled, `atlas_sql_generate`; use the returned
`contractVersion` for legacy compatibility, or prefer neutral contract `1.2`
for either dialect (see [neutral field names](neutral-contract.md)).

PostgreSQL URI:

```json
{
  "connectionString": "postgresql://reader:YOUR_PASSWORD@127.0.0.1:5433/sample_database",
  "schemas": ["sample_schema"]
}
```

PostgreSQL keyword/value string is also supported:

```text
host=127.0.0.1 port=5433 dbname=sample_database user=reader password='YOUR_PASSWORD'
```

Use PostgreSQL/libpq quoting in keyword strings, or percent-encode special characters
in URI credentials. Parsing uses Psycopg's existing conninfo parser. PostgreSQL 18
is the supported version. A password is required in caller-provided strings;
preconfigured connections can continue to use protected passfiles or secret references.

SQL Server uses a URI (the general ODBC connection-string format is not accepted):

```text
sqlserver://reader:YOUR_PASSWORD@sql.example.test:1433/database
```

`mssql://` is an alias. SQL Server still requires the optional driver and native
certification remains open. Remote connections require verified TLS. PostgreSQL
literal loopback addresses and `localhost` default to `sslmode=disable`; remote
connections default to `verify-full`. Credentials use driver keyword arguments or
escaped ODBC values; arbitrary driver options, socket paths, multi-host lists,
caller-selected local credential/certificate files and custom session options are
rejected. TLS trust comes from operator-provisioned certificates/environment.

## Lifetime and authorization

The server tests authentication, requires a restricted database reader and discovers
only accessible base-table columns through fixed catalog queries. No business rows
or generated SQL are executed. `schemas` optionally limits discovery to exact schema
names. Discovery is bounded to 10,000 columns and 1,000 tables; oversized discovery
fails explicitly. Subsequent calls recheck database privileges and metadata using
the same analyzers as preconfigured connections. Newly granted objects require a
new connection; permissions are never expanded automatically within a session.

Credentials stay in the server process, are excluded from reprs/results/errors and
never enter the SQL generation payload. Sessions are owned by the authenticated
principal, expire after one hour and are removed lazily on subsequent connection
operations. Restarting the process removes all sessions. Limits are ten sessions
per identity and 100 per process, in addition to existing request/deadline limits.
The MCP client receives the connection string as a tool argument; its conversation
history, tool recording and retention settings are outside this server's control.

## Start without a connection file

```sh
.venv/bin/atlas-sql-mcp
```

Existing `--connections /path/to/connections.json` registrations remain supported
and can coexist with connection strings. `127.0.0.1` always refers to the machine
running the MCP server.

Local stdio accepts caller-selected database endpoints. Shared HTTP retains bearer
authentication and disables connection-string networking by default. To enable it,
the operator supplies exact allowed `host:port` endpoints, separated by commas, in
`ATLAS_SQL_CONNECTION_HOSTS`, for example `db.example.test:5432`. Callers cannot
change that allowlist. An empty/unset list never permits arbitrary network access.

The new tool is announced when the server starts; reload the MCP connection after
upgrading an already-running server.
