# Register the MCP once

These templates start the installed MCP over stdio. Replace `/absolute/path/mcp-sql`
with the installation directory. No connection file, table list, business catalog,
or model API key is required for this workflow: the chat assistant writes
SQL using metadata discovered from the connection string.

| Client | Template |
| --- | --- |
| Codex with local Python | [codex.toml](codex.toml) |
| Codex with Docker | [codex-docker.toml](codex-docker.toml) |
| Claude Code | [claude-code.json](claude-code.json) |
| Cursor | [cursor.json](cursor.json) |

Merge the `atlas_sql` entry into the client's MCP settings, preserve existing entries,
and reload its server list. For container deployment, follow the [Docker guide](../docs/docker.md). These files are templates, not proof of manual
acceptance in every client version.

Users then provide a [connection string](../docs/connection-strings.md) and a request,
such as “Create SQL for monthly revenue, grouped by region, excluding canceled orders.”
The assistant connects, discovers accessible tables and columns, asks about ambiguous
business definitions, and validates its SQL. It returns SQL and typed filters; it does
not execute reports or read business records.

SQL Server additionally needs its optional dependencies and Microsoft ODBC Driver 18;
see the [SQL Server guide](../docs/sqlserver-support.md). Certificate trust belongs to
administrator setup. Avoid storing connection strings in client configuration files.

## Optional operator-managed policy

[connections.example.json](connections.example.json) illustrates the separate
`--connections /private/path/connections.json` compatibility mode. Its table names,
connection IDs and principal names are placeholders, not built-in customer knowledge.
This mode uses explicit column grants and external password references; it is unnecessary
when users supply connection strings. Keep populated files private and outside version
control. Local grant identities must match the actual OS account (`local#username`).
The server never creates these example databases or objects.

For the persistent local Docker container, use [the Docker Codex template](codex-docker.toml) and [persistent-container setup](../docs/docker.md#keep-a-local-container-running).

## Transport configuration

Local stdio uses the root Compose definition. The connected agent supplies the
LLM in every transport; the MCP has no model settings or provider credentials.

[compose.http.yaml](compose.http.yaml) enables shared authenticated HTTP using
`ATLAS_SQL_HTTP_ENV_FILE`, a private file for issuer, signing-key endpoint, public
URL and allowed database endpoints. It adds a local backend port and health probe.
Use the commands and environment reference in the [Docker guide](../docs/docker.md).
