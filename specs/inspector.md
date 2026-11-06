# Manual testing with MCP Inspector

The local Inspector is available at http://127.0.0.1:6274. Use the browser tab
opened by its launcher, which includes its private authentication token. The UI
and proxy bind to loopback. The MCP itself uses stdio only.

The server `atlas-sql` connects to the single persistent Docker process in
`atlas-sql-mcp`. It uses the exact bridge from the ignored local
`.env.inspector/mcp.json`: Transport STDIO, Command docker, its configured Args.
The bridge holds an exclusive client lock. Disconnect Inspector before CLI checks.

1. Connect `atlas-sql`. Reconnect after a container rebuild.
2. Open Tools, then `generate_sql`.
3. Fill only Question and click Execute Tool.
4. A generated result has status `generated`, one SELECT and
   `execution_performed: false`. No SQL is executed and no business rows return.
5. A business question missing essential definitions can return status
   `needs_clarification`. Submit a new complete question containing the answer;
   this server does not remember earlier calls. Infrastructure failures remain
   Tool Errors and can be correlated with Docker logs.

## Two questions to paste into Question

1. How many application clients are configured in the ERP?
2. List up to 10 active application clients ordered by name.

These requests require no database identifiers. The current connection is the
existing Docker iDempiere ERP database. Metadata inspection found 912 base tables
in its nonempty application schema; the empty default schema is ignored. No
synthetic runtime database or fixed table allowlist is configured. The server
chooses relevant tables afresh, within documented resource budgets. It generates
SQL; business meaning and successful database execution are not certified.

“Give me a 360-degree view of each company” does not define which company role or
metrics belong in the view. If clarification is needed, specify business concepts
such as application clients, organizations, customers or suppliers, together with
the desired information and period. Never supply a table name to resolve this.

## Configuration and restart

The repository has one root `.env`, containing runtime and Compose settings.
It is ignored and private; no alternate environment file is used. The existing
ERP project's own files and private connection files remain intact. Compose joins
the ERP network and mounts the existing database CA read-only for verified TLS.

```sh
docker compose up -d --wait
npx -y -p node@24.22.1 -p @modelcontextprotocol/inspector@2.14.0 mcp-inspector --web --config .env.inspector/mcp.json --server atlas-sql
```

Do not launch a second Inspector if the existing one is running. Its token and
local logs must stay private. To inspect MCP diagnostics:

```sh
docker compose logs -f mcp
```

Alternatively, Docker Desktop → atlas-sql-mcp → Logs. Request IDs correlate
metadata, selection, generation, validation and clarification/failure events.
Questions, SQL, prompts, credentials and raw dependency errors are excluded.

Reference: https://github.com/modelcontextprotocol/inspector
