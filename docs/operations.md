# Operations, limits and production acceptance

## Runtime responsibilities

HTTP exposes `GET /healthz` returning only `ok` with no database or identity data.
This is process liveness, not database readiness. The endpoint requires no
bearer token; keep the backend behind your configured ingress/network boundary.
Other HTTP MCP routes retain their authentication requirements. Preserve the public
Host header when probing the backend if transport Host validation applies.

Run with a restricted database reader and operator-provisioned certificate trust.
Use [Docker deployment](docker.md) for the packaged PostgreSQL/SQL Server runtime.
Process logs and metrics must exclude connection strings, passwords, tool arguments,
and generated request content unless an approved redaction/retention policy exists.
Monitor process health, request failures, timeouts, resource use and expiry/reconnect
frequency. The service writes protocol messages to stdout in stdio mode.

Credentials and connection/context handles live only in process memory. Expired credentials are purged by a 30-second background task and all sessions are
cleared at graceful shutdown. A restart requires users to reconnect. Replicas need session affinity; admission and context
budgets are per process. A gateway must enforce deployment-wide limits. The MCP
does not persist customer schema catalogs, report results or provider credentials.

## Implemented analytical support and limits

Automated checks cover qualified base tables; INNER/LEFT/RIGHT/FULL joins; correlated subqueries and a scalar LATERAL aggregate; CTEs and recursive CTEs; UNION/INTERSECT/EXCEPT; grouping sets/ROLLUP/CUBE; DISTINCT ON; windows and frames; numeric/conditional/date/timezone expressions; filtered aggregates; JSONB and array operations; typed filters; and a monthly cohort query exceeding 1,000 lines reconciled against independent SQL.

PostgreSQL-native parsing uses pglast/libpg_query 18.6. SQLGlot supplies scoped resolution and lineage, with PostgreSQL-specific aggregate/text/date/type corrections and conservative semantic checks. Both representations must agree on base relation dependencies. Analysis runs in a disposable subprocess with a seven-second deadline and CPU limits; Linux also enforces an address-space limit. Each metadata operation has a five-second cancellation budget; requests and finalization have a twenty-second total budget. Typed metadata is loaded only for selected pages and rechecked for the inspected scopes used by SQL. This Python package was chosen to use the native PostgreSQL parser without adding a parser sidecar to the Java reporting process; see [the decision record](archive/sql-mcp-parser-decision.md).

The implementation deliberately rejects unsupported constructs rather than bypassing analysis:

- Views/materialized views without dependency attestation; custom types/functions/operators; table-valued functions; NATURAL/USING/CROSS joins; projection wildcards; and cross-database references.
- Nullable runtime parameters, data-driven enumeration lookups, and configured machine-readable business measures. Comments have explicit untrusted provenance; business meanings require host confirmation.
- Unprovable join fan-out for sensitive aggregates. The analyzer uses unique-key paths, including composite keys and projected/preaggregated keys. It may reject safe queries when the necessary proof is unavailable. It does not repair totals with SUM(DISTINCT).
- Features outside the tested type/overload subset. This is not PostgreSQL's full server semantic analyzer, an optimizer, or a proof of natural-language/business equivalence. Successful static validation can still have runtime/data errors or unacceptable execution cost. The artifact states those limits.

Limits: 256 KiB SQL, 512 KiB complete input, 1 MiB complete encoded MCP result including its text fallback, 100,000 AST traversal nodes, nesting depth 128, and 1,000 table references. Metadata pages default to 25 relations (maximum 100). Oversized SQL/results fail explicitly without truncation. Contexts are identity-bound, expire after ten minutes, and are bounded to 25 per identity and 1,000 globally; cursors are bounded to 5,000 globally. Request admission is 60 calls per identity per minute and 10 concurrent calls per process. These are per-process limits, not distributed admission control; use one worker or enforce additional limits at a shared gateway.

## Large database discovery

Connection discovery reads a lightweight, authorized index of table and column
names. It does not load every table's types and constraints. Databases are not
rejected simply for having more than 1,000 tables or 10,000 columns.

For more than 100 accessible relations, ordinary context pages contain names
only. The agent can search those names and column names, then request exact
`focus.relations` for typed details. `focus.include: []` explicitly requests a
names-only inventory for any database. The agent must inspect every base table
and inherited dependency used by its SQL. Finalization uses an isolated parser
to select only previously inspected scopes, rechecks their live metadata before
and after analysis, and sends only the query's dependencies to the analyzer.
Unsupported objects elsewhere in the database do not prevent a scoped report.
Foreign keys are disclosed only when both tables are in the inspected scope.

Resource controls bound metadata memory rather than report result rows: discovery
streams batches of 512 names, with a 16 MiB transfer budget. Retained connection
metadata has a conservative 16 MiB estimate per session and 64 MiB per process;
capacity is released on expiry. Contexts retain fingerprints, with at most 100
detail-page groups each. Analyzer input is bounded to 2 MiB of SQL plus selected
metadata. Capacity failures are explicit and never truncate report SQL or data.

## Report completeness

There is no report result-row limit or SQL line-count limit. Metadata page sizes
limit schema discovery pages, not report rows. The MCP never inserts LIMIT, TOP,
pagination or sampling on its own. Explicit top-N requests retain their bounds.
SQL and payload size/complexity budgets still apply: oversized requests fail
explicitly without truncating a query. A downstream executor owns result streaming,
timeouts, resource limits, permissions and parameter binding.

## Before deploying to customers

1. Build and verify the release image; deploy an immutable image digest.
2. Verify restricted database roles and certificate trust against each destination.
3. For shared HTTP, validate ingress Host preservation, OAuth issuer/JWKS/audience,
   allowed database endpoints, timeout settings and client onboarding end to end.
4. Reconcile representative customer requests and ambiguous business definitions
   with actual users. Deterministic simulations are not live model evaluations.
5. Measure concurrency, latency and resource use under the expected deployment load;
   plan session affinity and reconnect behavior for restarts and scaling.
6. Have the report executor reauthorize dependencies, bind all parameters, honor
   required session settings, and apply its own execution policy.

See [test layers and recorded evidence](testing.md) for what was actually verified.
Full engine syntax coverage, arbitrary customer ERP schemas and sustained
customer production-load behavior remain unverified. Successful static validation does not
prove business equivalence, runtime success or efficient execution plans.
