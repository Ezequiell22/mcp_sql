# Tool and artifact contract

Prefer **contract 1.2** for both supported dialects: `sqlType`,
`databaseMajorVersion`, and `databaseMajorVersions` are database-neutral.
Legacy 1.0/1.1 remain compatible. See the [migration and exact input fields](neutral-contract.md).

`atlas_sql_connect` accepts a connection string and returns `connectionId`, dialect,
contract version and expiry. Existing configured connection IDs remain supported.

1. `atlas_sql_context_get`: `contractVersion: "1.2"`, `connectionId`, and natural-language `request`; optional focused search, relation list, included details, and page size. Returns an opaque ten-minute handle, authorized catalog, constraints, capabilities, metadata/policy fingerprints, clock/timezone, coverage, and a cursor. Follow-up calls retain request, connection, context, and exact focus/page size when following a cursor.
2. `atlas_sql_finalize`: `contextId`, host-written `sql`, `parameters`, `filters`, `outputs`, and `semantics`. Rechecks identity, schema, policy, syntax, nested dependencies, supported types, join grain risks, binding and output contracts, and freshness again after analysis. Returns a portable artifact or a safe structured error. It cannot infer every unresolved business choice; the host asks the user before calling it.

Large live schemas return an index first: `coverage: "partial"`, empty typed
`columns`/`relationships`, and guidance in `omittedDetails`. Inspect exact
`focus.relations` in the same context before finalizing SQL that uses them.
Use related tables in the same group to retrieve their relationship evidence.
`focus.include: []` requests names only and does not count as typed inspection.
When following a cursor, preserve the original focus and page size; when selecting
a different group, keep the context/request/connection and omit the old cursor.
`CONTEXT_INCOMPLETE` means the required typed scope has not been inspected;
`CONTEXT_STALE` means metadata or permissions changed and a fresh context is needed.
For live indexed contexts, the context fingerprint identifies the authorized
name/column-privilege snapshot; the artifact dependency fingerprint identifies
the typed dependency metadata actually validated. These scopes differ, so do not
require equality between the two fingerprints. Consumers must reauthorize the
artifact dependencies before execution.

[Context request](../examples/context.json) and [finalization request](../examples/finalize.json) examples match the implemented schemas. Replace the finalization example's context ID with the returned handle and confirm its schema/business assumptions before use. The connected agent constructs these requests, writes the SQL and handles the discovery/validation loop. The server has no LLM or generation endpoint.

[Executable schemas](../schemas/) are exported from the same definitions the server validates. After intentionally changing the contract, regenerate with `python -m atlas_sql_mcp.contracts schemas`; `--check` detects stale exports. The [specification example](archive/sql-generation-mcp-spec.md#output-and-binding-contract) explains the full target artifact; the exported schemas are the implemented wire contract.

For PostgreSQL contract 1.0, all runtime values use explicitly cast `$1`, `$2`, … parameters. SQL Server contract 1.1 uses `CAST(@p1 AS type)` and `sqlType` fields; see the [SQL Server guide](sqlserver-support.md). The filter array supplies each position's logical name, SQL type, lossless value format and field binding; the caller must bind values rather than interpolate them. Every parameter occurrence must affect a WHERE/HAVING/JOIN predicate. Temporal ranges use inclusive `from` and exclusive `until`; declarations are checked against their SQL comparison operators. Decimal and integer defaults/bounds are strings to retain precision. Nullable runtime parameters are explicitly unsupported in this version. Array filters declare an exact element type; empty arrays used with `ANY` match no rows, and null elements are not supported by the binding contract.

Output labels and assumptions are host declarations. Result names/types/lineage come from analysis. `nullable: null` means unknown, not nonnullable. The `artifactHash` covers SQL, bindings, outputs, semantic declarations and schema/policy dependencies; time-dependent validation evidence is excluded. SQL is preserved verbatim. PostgreSQL consumers must honor `requiredSessionSettings.search_path = pg_catalog`;
SQL Server consumers must apply its declared session settings. All consumers must reauthorize dependencies and freshness, and apply their own execution limits before running it. The artifact is not an execution grant.
