# MCP SQL — Atlas

Standalone PostgreSQL and SQL Server SQL-generation support for the LLM in Codex, Claude, or another MCP host. It reads authorized database catalog metadata and validates host-written SQL. It returns the original SQL, typed parameters, filters, result metadata, lineage, assumptions, and validation evidence. It preserves two catalog/validation tools and optionally exposes `atlas_sql_generate` through a small LangChain pipeline. It has no report lifecycle, query execution, or dependency on the Java reporting service.

This repository contains only the SQL-generation core. Reporting applications, web interfaces, persistence and query execution are separate consumers, with their own authorization and execution policies. The [target specification](docs/sql-generation-mcp-spec.md) includes broader acceptance gates; see the implementation limits below before treating the package as a complete PostgreSQL server analyzer.

## LangChain: text to validated SQL

Set `ATLAS_SQL_MODEL` to enable `atlas_sql_generate`: request clarification/refinement,
structured SQL generation, deterministic validation, and at most three drafts.
The original request remains authoritative. Ambiguities return questions; queries
are never executed. Only the required LangChain components are installed.
See [setup, prompts, limits and verification](docs/langchain-generation.md).

## SQL Server and one-query generation

PostgreSQL 18 retains contract 1.0. A separate **experimental SQL Server adapter** targets major versions 16/17 using contract 1.1, native T-SQL type declarations and `CAST(@p1 AS type)` parameters. It has isolated static analysis, fixed catalog queries, per-column permission checks and verified TLS. Its accepted subset includes joins, CTEs, aggregation, CASE/COALESCE and ranking. Unsupported syntax/types fail explicitly. It has not yet passed native SQL Server/ODBC certification; artifacts state `passed_static_subset` rather than claiming server-native syntax verification.

See [SQL Server setup, capabilities, bounded host repair workflow and release gates](docs/sqlserver-support.md), [connection configuration](examples/connections-sqlserver.json), and [finalization example](examples/finalize-sqlserver.json). The host generates one complete SQL statement and may repair structured errors for up to three attempts. Without optional generation configured, the MCP exposes the original two tools and makes no model calls. An agent loop cannot by itself prove business correctness or an efficient query plan.

Run `.venv/bin/python scripts/verify.py` for Python syntax, local documentation links, schema consistency and isolated tests. The offline `atlas-sql-check --dialect sqlserver query.sql` diagnostic checks syntax only and never issues an artifact. SQL Server dependencies are optional and pinned in [requirements-sqlserver.lock](requirements-sqlserver.lock).

## Install and test

Requires Python 3.12 or newer on macOS or Linux. PostgreSQL 18 is the currently tested destination version. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/pytest tests
.venv/bin/python -m atlas_sql_mcp.contracts schemas --check
```

The base lock file pins PostgreSQL runtime and test dependencies. SQL Server additionally requires its optional lock file and the Microsoft ODBC Driver. The native parser provides prebuilt wheels for supported platforms; a platform without a compatible wheel may need a C build toolchain. Tests exercising the database are skipped unless explicitly enabled. They create a **new disposable Docker PostgreSQL 18 container**, populate only that fixture, use a restricted reader for product calls, then remove that container. They cannot take an existing database/container as a target:

```sh
ALLOW_SYNTHETIC_DB_WRITES=1 .venv/bin/pytest tests
```

Ensure the `postgres:18` image is available to Docker. Test fixture writes do not authorize customer database changes. The product adapter itself has only fixed catalog queries and never executes generated SQL, PREPARE, EXPLAIN, or business-row lookups.

## Configure a local connection

Copy [the connection example](examples/connections.json) to a private file outside the repository, or the ignored `connections.local.json`. Replace the database details and `local#REPLACE_WITH_OS_USERNAME` with `local#` followed by the actual OS username. The example names and tables are illustrative; this package does not create them.

- `grants` explicitly maps an authenticated principal to schema-qualified tables and exact allowed columns. Wildcard grants are rejected. Database permissions are intersected with these grants.
- `passwordEnv` names an externally provisioned variable; its value never enters a tool argument or result. An empty value selects libpq's normal credential lookup, including an OS-protected passfile via `PGPASSFILE`. Do not place a password in the JSON or MCP tool arguments.
- Local stdio always uses the OS account as its identity. There is no tool argument or CLI flag that claims another local user.
- Remote PostgreSQL hosts require `sslmode: "verify-full"` and the appropriate trusted certificate configuration. Disabling TLS is accepted only for literal loopback addresses.
- The role must be neither superuser nor BYPASSRLS. It must be able to read permitted relation/column metadata and possess SELECT privileges for the permitted columns. Do not use the migration owner.
- The connection file is reloaded on every catalog request, including finalization checks. Invalid/revoked policy fails closed; a restart is not needed to revoke a grant. Secret references remain operator configuration.
- No destination `reporting.database_identity`, helper view, Atlas schema, or reporting metadata migration is required.

Start local stdio:

```sh
.venv/bin/atlas-sql-mcp --connections /absolute/path/connections.local.json
```

MCP uses stdout for protocol messages. Register the absolute executable/config paths in the client using [the Codex example](examples/codex.toml) or [the Claude example](examples/claude.json). Ensure the client process can access the protected credential source. These are configuration templates, not claims that those specific applications have passed manual acceptance testing.

## Shared Streamable HTTP

Shared mode requires bearer authentication; there is no unauthenticated HTTP mode. Provision a trusted HTTPS ingress and an OAuth issuer that issues RS256 JWT access tokens with:

- `iss` matching `ATLAS_SQL_ISSUER`;
- `aud` matching the full `ATLAS_SQL_PUBLIC_URL`, including `/mcp`;
- valid `exp`, a nonempty `sub`, and scope `atlas:sql`.

Set `ATLAS_SQL_JWKS_URL` to the trusted issuer's HTTPS JWKS endpoint. Configure connection grant identities as `issuer#subject`, for example `https://identity.example.test#analyst`. Issuer URLs cannot contain fragments. Then run:

```sh
.venv/bin/atlas-sql-mcp \
  --connections /absolute/path/shared-connections.json \
  --transport streamable-http --host 127.0.0.1 --port 8091
```

`ATLAS_SQL_PUBLIC_URL` must be an HTTPS URL ending in `/mcp`. Its authority is the allowed Host; supplied browser Origins are rejected. The trusted ingress must preserve that Host, terminate TLS, restrict backend reachability, and enforce request/read timeouts. OAuth protected-resource metadata is served at `/.well-known/oauth-protected-resource/mcp`. Configure public client registration/PKCE at the issuer; the service does not provision identity-provider clients. JWKS metadata is cached for up to five minutes; token expiry/signature/audience/issuer and scope checks apply on every request.

Only `ATLAS_SQL_TIMEZONE` is optional (default `UTC`, valid IANA timezone). SQL must use explicit date/timezone decisions; this setting does not rewrite SQL. Shared transport has automated SDK tests with signed tokens; real hosted-client onboarding, ingress deployment, and issuer integration remain acceptance work for the chosen environment.

## Tools and portable contract

1. `atlas_sql_context_get`: `contractVersion: "1.0"`, `connectionId`, and natural-language `request`; optional focused search, relation list, included details, and page size. Returns an opaque ten-minute handle, authorized catalog, constraints, capabilities, metadata/policy fingerprints, clock/timezone, coverage, and a cursor. Follow-up calls retain request, connection, context, and exact focus/page size when following a cursor.
2. `atlas_sql_finalize`: `contextId`, host-written `sql`, `parameters`, `filters`, `outputs`, and `semantics`. Rechecks identity, schema, policy, syntax, nested dependencies, supported types, join grain risks, binding and output contracts, and freshness again after analysis. Returns a portable artifact or a safe structured error. It cannot infer every unresolved business choice; the host asks the user before calling it.

[Context request](examples/context.json) and [finalization request](examples/finalize.json) examples match the implemented schemas. Replace the finalization example's context ID with the returned handle and confirm its schema/business assumptions before use. In host mode, the client constructs these requests. In LangChain mode, the server orchestrates the same context and validation calls.

[Executable schemas](schemas/) are exported from the same definitions the server validates. After intentionally changing the contract, regenerate with `python -m atlas_sql_mcp.contracts schemas`; `--check` detects stale exports. The [specification example](docs/sql-generation-mcp-spec.md#output-and-binding-contract) explains the full target artifact; the exported schemas are the implemented wire contract.

For PostgreSQL contract 1.0, all runtime values use explicitly cast `$1`, `$2`, … parameters. SQL Server contract 1.1 uses `CAST(@p1 AS type)` and `sqlType` fields; see the dialect guide above. The filter array supplies each position's logical name, SQL type, lossless value format and field binding; the caller must bind values rather than interpolate them. Every parameter occurrence must affect a WHERE/HAVING/JOIN predicate. Temporal ranges use inclusive `from` and exclusive `until`; declarations are checked against their SQL comparison operators. Decimal and integer defaults/bounds are strings to retain precision. Nullable runtime parameters are explicitly unsupported in this version. Array filters declare an exact element type; empty arrays used with `ANY` match no rows, and null elements are not supported by the binding contract.

Output labels and assumptions are host declarations. Result names/types/lineage come from analysis. `nullable: null` means unknown, not nonnullable. The `artifactHash` covers SQL, bindings, outputs, semantic declarations and schema/policy dependencies; time-dependent validation evidence is excluded. SQL is preserved verbatim. Consumers must honor `requiredSessionSettings.search_path = pg_catalog`, reauthorize dependencies and freshness, and apply their own execution limits before running it. The artifact is not an execution grant.

## Implemented analytical support and limits

Automated checks cover qualified base tables; INNER/LEFT/RIGHT/FULL joins; correlated subqueries and a scalar LATERAL aggregate; CTEs and recursive CTEs; UNION/INTERSECT/EXCEPT; grouping sets/ROLLUP/CUBE; DISTINCT ON; windows and frames; numeric/conditional/date/timezone expressions; filtered aggregates; JSONB and array operations; typed filters; and a monthly cohort query exceeding 1,000 lines reconciled against independent SQL.

PostgreSQL-native parsing uses pglast/libpg_query 18.6. SQLGlot supplies scoped resolution and lineage, with PostgreSQL-specific aggregate/text/date/type corrections and conservative semantic checks. Both representations must agree on base relation dependencies. Analysis runs in a disposable subprocess with a seven-second deadline and CPU limits; Linux also enforces an address-space limit. Whole catalog reads have a three-second cancellation budget; finalization issues no success after its ten-second total budget. This Python package was chosen to use the native PostgreSQL parser without adding a parser sidecar to the Java reporting process; see [the decision record](docs/sql-mcp-parser-decision.md).

The implementation deliberately rejects unsupported constructs rather than bypassing analysis:

- Views/materialized views without dependency attestation; custom types/functions/operators; table-valued functions; NATURAL/USING/CROSS joins; projection wildcards; and cross-database references.
- Nullable runtime parameters, data-driven enumeration lookups, and configured machine-readable business measures. Comments have explicit untrusted provenance; business meanings require host confirmation.
- Unprovable join fan-out for sensitive aggregates. The analyzer uses unique-key paths, including composite keys and projected/preaggregated keys. It may reject safe queries when the necessary proof is unavailable. It does not repair totals with SUM(DISTINCT).
- Features outside the tested type/overload subset. This is not PostgreSQL's full server semantic analyzer, an optimizer, or a proof of natural-language/business equivalence. Successful static validation can still have runtime/data errors or unacceptable execution cost. The artifact states those limits.

Limits: 256 KiB SQL, 512 KiB complete input, 1 MiB complete encoded MCP result including its text fallback, 100,000 AST traversal nodes, nesting depth 128, and 1,000 table references. Metadata pages default to 25 relations (maximum 100). Oversized SQL/results fail explicitly without truncation. Contexts are identity-bound, expire after ten minutes, and are bounded to 25 per identity and 1,000 globally; cursors are bounded to 5,000 globally. Request admission is 60 calls per identity per minute and 10 concurrent calls per process. These are per-process limits, not distributed admission control; use one worker or enforce additional limits at a shared gateway.

## Evidence and remaining release gates

Verification recorded on 26 October 2026: **401 automated checks passed**, including 87 checks against a newly created disposable PostgreSQL 18 fixture. The large-query case contains **1,462 lines / 57,571 UTF-8 bytes** and matches an independent reference query. The fixed-width currency-code regression is included. The package also builds as a wheel and its isolated parser runs from a fresh installation outside the repository. Source lint/format, exported-schema consistency, documentation links, and whitespace checks pass. Six optional native SQL Server checks were skipped; the SQL Server ODBC/TLS and engine release gates remain open. These are local conformance results, not the target's client/model or load-test measurements.

Automated tests cover native/scoped analysis, independent PostgreSQL output types, selected business-result reconciliation, large-query integrity, output schemas, filter failures, authorization drift, cross-user contexts, real stdio subprocess calls, shared HTTP SDK calls, and signed-token failures. They do not establish real Codex/Claude model orchestration quality, a ten-user performance percentile, all PostgreSQL overload semantics, attested views, or report-consumer migration. Those target-spec gates remain open. Keep the existing reporting registration separate until its consumer explicitly supports an artifact's query features.
