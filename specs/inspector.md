# MCP Inspector

Use stdio. For a local process, command `.venv/bin/python`, arguments
`-m MCP.server`, working directory at the repository root.

For the existing persistent Docker listener, use command `docker` and these
arguments (the shell command is one final argument):

```json
["exec","-i","atlas-sql-mcp","flock","-n","-E","75","/tmp/atlas-mcp/client.lock","sh","-c","cat /tmp/atlas-mcp/output & reader=$!; trap 'kill \"$reader\" 2>/dev/null || true' EXIT; cat > /tmp/atlas-mcp/input"]
```

The ignored `.env.inspector/mcp.json` may already contain this bridge. It remains
private. Disconnect the current client before attaching another. Do not start a
second Python listener with docker exec. Reconnect after replacing the container.

Open Tools → `execute_select`, fill **sql**, then execute. First check:

```sql
SELECT 1 AS connection_check
```

Expect columns `["connection_check"]`, rows `[[1]]`, row_count `1`.
Natural-language questions are no longer accepted. The calling project must
provide SQL and business/schema knowledge. Use an explicit filter or row limit
when testing a business query; the server returns all rows from the submitted SQL.

The current private deployment's database URL, reader, network and public CA are
configured through the ignored root `.env`. This refactor preserves those files
and Compose's certificate installation. It does not certify their permissions or
execute queries against the configured business database. Rebuild and restart
explicitly when ready to switch that running deployment to the new contract.
