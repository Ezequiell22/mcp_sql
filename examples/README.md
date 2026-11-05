# Tool payload examples

These files illustrate MCP tool contracts. The agent constructs actual requests;
end users need only a connection string and a natural-language question.

| File | Purpose |
| --- | --- |
| [connect.json](connect.json) | Establish a connection-string session |
| [context.json](context.json) | Fetch metadata with a legacy PostgreSQL contract |
| [finalize-neutral.json](finalize-neutral.json) | Finalize using neutral contract 1.2 |
| [finalize.json](finalize.json) | Legacy PostgreSQL finalization |
| [finalize-sqlserver.json](finalize-sqlserver.json) | Legacy SQL Server finalization |

Replace connection/context handles with live returned values and table/column names
with real discovered metadata. Illustrative schemas and business assumptions are
not production dependencies. Prefer [contract 1.2](../docs/neutral-contract.md) for
new integrations. Host registration and optional administrator connection templates
live only in [configs](../configs/README.md).
