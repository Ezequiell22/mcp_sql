# Database-neutral contract 1.2

Use `contractVersion: "1.2"` with `atlas_sql_context_get` and `atlas_sql_finalize`
for either supported database dialect.
Use the connection ID returned by `atlas_sql_connect` or an authorized configured ID;
connection names, schemas, tables and business models are never fixed by this contract.

Both dialects use `sqlType` in columns, parameters and derived outputs,
`databaseMajorVersion` in context/artifacts, and `databaseMajorVersions` in capabilities.
The connection still selects the parser, SQL syntax, permitted types, supported
versions and required session settings. This does not add support for other engines.
Legacy PostgreSQL 1.0 and SQL Server 1.1 remain unchanged.

Finalization accepts exactly `contractVersion`, `contextId`, `sql`, `parameters`,
`filters`, `outputs`, and `semantics`. Do not send `connectionId` or `explanation`.
Output declarations contain `key`, `label`, and optional `sqlType` and `unit`.
The validator derives `lineage`, `logicalType`, and `nullable`; these are response
fields, not accepted declarations. The original failing requests mixed response
fields into input and supplied unsupported top-level fields.

See [the neutral finalization example](../examples/finalize-neutral.json).
Replace its context ID with a fresh context obtained using version 1.2, and replace
its illustrative table/column names with objects from your authorized catalog.
Artifact hashes cover the neutral representation. Consumers must not compare a
legacy artifact hash with a 1.2 artifact hash as though their contracts were identical.
Generated SQL remains read only and is never executed by the MCP.

## Verification

Regression coverage includes neutral PostgreSQL and SQL Server metadata,
parameter/type checking, artifact hashes, SQL preservation, arbitrary catalog names,
aggregation and authenticated agent-driven MCP validation with legacy and neutral contracts.
See [current test evidence and reproduction](testing.md); older test counts do not
represent the current checkout. Reload existing MCP clients after an upgrade to
rediscover tool schemas.
