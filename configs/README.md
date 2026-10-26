# Import Atlas SQL into your MCP client

These files are configured for the local checkout at `/Users/ezequielmenegas/git/mcp-sql`. They launch `.venv/bin/atlas-sql-mcp` over stdio using the private `connections.local.json` file in the repository root. If you move the checkout, update `command` and the connections path in all three files.

## Choose your client

| Client | Prepared file | Installation |
| --- | --- | --- |
| Codex | [codex.toml](codex.toml) | Merge its `[mcp_servers.atlas_sql]` section into `~/.codex/config.toml`, or the target project's `.codex/config.toml`. |
| Claude Code | [claude-code.json](claude-code.json) | Merge its `atlas_sql` entry into `.mcp.json` at the target project root. For a session, use the command below. |
| Cursor | [cursor.json](cursor.json) | Merge its `atlas_sql` entry into `~/.cursor/mcp.json` globally, or the target project's `.cursor/mcp.json`. |

Preserve other MCP entries when merging. These prepared files have not overwritten or registered anything in your clients' existing settings.

For Claude Code, load the file directly for a session:

```sh
claude --mcp-config /Users/ezequielmenegas/git/mcp-sql/configs/claude-code.json
```

After saving persistent configuration, restart the client or reload its MCP server list. Expect exactly `atlas_sql_context_get` and `atlas_sql_finalize`. The client may ask you to trust the server/project.

Formats and locations were checked against the official [Codex MCP documentation](https://developers.openai.com/codex/mcp), [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp), and [Cursor MCP documentation](https://cursor.com/docs/mcp).

## Complete the database configuration

A private `connections.local.json` was initialized with placeholders if it did not already exist. It is ignored by Git and was created with owner-only permissions. [connections.example.json](connections.example.json) is the matching template. No real database credentials are included.

Before requesting database context:

1. Edit the private `connections.local.json`: set the actual host, database, database reader user, schemas, tables and exact permitted columns. Remove whichever example connection you do not use. PostgreSQL requires version 18; SQL Server's experimental adapter targets major 16/17.
2. Keep `local#ezequielmenegas` as the local grant identity on this Mac. It represents the OS user running the MCP, independently of the configured database login. Change it if the MCP runs under another OS account.
3. Provision `ATLAS_SALES_DB_PASSWORD` for PostgreSQL and/or `ATLAS_SQLSERVER_PASSWORD` for SQL Server in the environment of the client process. The JSON clients inherit their launch environment; Codex explicitly forwards the names in `env_vars`. A GUI app may not inherit variables exported in a separate terminal. Do not paste passwords into these client files.
4. Keep verified TLS and configure trust for the destination's certificate. For SQL Server, install `requirements-sqlserver.lock` and Microsoft ODBC Driver 18 as described in [the SQL Server guide](../docs/sqlserver-support.md).

The PostgreSQL connection ID is `sales-readonly` and uses `contractVersion: "1.0"`. The SQL Server connection ID is `sales-sqlserver` and uses `contractVersion: "1.1"`. One MCP registration serves both connections. The database adapter is selected by the operator configuration.

A successful startup/tool-list check does not test database connectivity. The placeholder destinations cannot supply metadata until you replace them. This MCP constructs and validates SQL; it does not run generated queries.
