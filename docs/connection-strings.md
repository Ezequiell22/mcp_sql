# Connect with a connection string

Users can paste their connection string and ask a question in ordinary language.
The assistant calls `atlas_sql_connect` with `connectionString` and handles the
remaining tool arguments internally. Surrounding paste whitespace is ignored. No connection configuration file
or predeclared connection ID is required. The returned `connectionId` is used by
`atlas_sql_context_get`; use the returned
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

SQL Server accepts a URI:

```text
sqlserver://reader:YOUR_PASSWORD@sql.example.test:1433/database
```

`mssql://` is an alias. You can also paste a SQL Server keyword string:

```text
Server=tcp:sql.example.test,1433;Database=database;UID=reader;PWD={YOUR_PASSWORD};Encrypt=yes;TrustServerCertificate=no;
```

`Data Source`, `Initial Catalog`, `User ID` and `Password` are accepted aliases.
The optional `Driver` must be `{ODBC Driver 18 for SQL Server}`. Keywords are
case insensitive; duplicate keys (including aliases) fail explicitly. For values
containing semicolons, use braces and escape a closing brace as `}}`.
The default TCP port is 1433. Named instances require their explicit TCP host/port;
DSNs, integrated/Entra authentication and arbitrary connection options are unsupported.
Encryption and certificate verification are always required; insecure settings
are rejected rather than silently changed. This is a supported subset of
[Microsoft's ODBC keywords](https://learn.microsoft.com/en-us/sql/connect/odbc/dsn-connection-string-attribute).
SQL Server requires the optional driver; native and verified-TLS workflow evidence
is documented in the [SQL Server guide](sqlserver-support.md). Remote connections require verified TLS. PostgreSQL
literal loopback addresses and `localhost` default to `sslmode=disable`; remote
connections default to `verify-full`. Credentials use driver keyword arguments or
escaped ODBC values; arbitrary driver options, socket paths, multi-host lists,
caller-selected local credential/certificate files and custom session options are
rejected. TLS trust comes from operator-provisioned certificates/environment.

## Lifetime and authorization

The server tests authentication, requires a restricted database reader and discovers
only accessible base-table columns through fixed catalog queries. No business rows
or generated SQL are executed. `schemas` optionally limits discovery to exact schema
names. Discovery streams an authorized name index, without a fixed database-wide
table or column count ceiling. Typed details are fetched for inspected report
scopes; metadata memory budgets remain explicit. Subsequent calls recheck database privileges and metadata using
the same analyzers as preconfigured connections. Newly granted objects require a
new connection; permissions are never expanded automatically within a session.

Credentials stay in the server process, are excluded from reprs/results/errors and
never enter the SQL generation payload. Sessions are owned by the authenticated
principal and expire after one hour. The server removes expired credentials every
30 seconds
even while idle, and clears sessions during graceful shutdown. Connection operations
also reject expiry immediately. Restarting the process removes all sessions. Limits are ten sessions
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
