# Tech lead review — November 6, 2026

## Resolution after implementation

The original findings below describe the pre-fix working tree. They have been
addressed within the explicit production contract in [production.md](production.md):

- Connection policy and SQL parser mapping now explicitly cover PostgreSQL,
  SQL Server and MySQL. MySQL has a pinned driver, verified TLS, read-only metadata
  sessions and disposable integration coverage. Unsupported families fail closed;
  universal database compatibility is not claimed.
- Metadata discovery includes views, PostgreSQL materialized views and multiple
  selected schemas. Cross-schema foreign keys are preserved. Both the MCP tool
  and generator accept only question. Legacy schema/table arguments and their
  validation messages are removed. There is no fixed selected-table count cap;
  a regression selects 25 real SQLite tables. Byte/time budgets still apply.
- Missing physical-table schemas are qualified only with the connection's verified
  native default. PostgreSQL public, custom reader defaults, SQL Server dbo,
  MySQL database defaults and CTE aliases are covered. Existing explicit schemas
  are not rewritten and defaulting never authorizes an uninspected object.
- Unclassified/qualified function calls, sequences, table/query hints and executable
  MySQL comments are rejected. Parser errors now use SQLGlot's ErrorLevel enum;
  the previous string silently allowed some parse errors. These additional gaps
  were found during the implementation review and have regression coverage.
- Production generation runs in a fresh terminable worker with a parent deadline,
  bounded cleanup and immediate busy rejection. A watchdog covers partial
  IPC replies as well as blocked database/provider calls. No request scope survives it.
- Container health checks a fresh event-loop heartbeat and live listener PID.
  A disposable Docker test suspends the listener and verifies unhealthy status
  despite the remaining FIFO. Process-failure restart policy is explicit.
- Optional LangSmith tracing is disabled and reflection warnings are suppressed
  at the production entry point. Real metadata and DeepSeek remain the production
  defaults; test doubles stay in tests.

Actual final test results and remaining limits are recorded in verification.md.
This is a reviewed implementation for the documented compatibility matrix, not
certification of any target environment or arbitrary read-only SQL.

## Original verdict

Not ready for production under the requested database-agnostic, general read-only
SQL contract. This review examines the current working tree, including existing
uncommitted changes; it does not approve deployment. No runtime code was changed.

## Original findings, ordered by priority

1. **P1 — Switching to another database family requires code changes.**
   `MCP/settings.py:26` permits only PostgreSQL/psycopg and SQL Server/pyodbc.
   `MCP/generator.py:166` chooses connection arguments through a PostgreSQL/other
   branch; line 443 maps every non-MSSQL engine to the PostgreSQL parser. Merely
   removing the configuration allowlist would therefore be incorrect. Add an
   explicit supported-dialect capability mapping covering drivers, connection
   policy, reflection and parsing, with integration evidence for each supported
   family. Universal database compatibility is not a defensible promise.

2. **P1 — Valid read-only requests are excluded by scope discovery and validation.**
   `MCP/generator.py:294` discovers base tables only; lines 334–337 select exactly
   one schema, and lines 118–128 reject other schemas. Views and cross-schema joins
   cannot work even on the two supported database families. Limits of 20 tables,
   80 columns per table and 64 KB context further preclude “any query.” Support
   views and per-request multi-schema metadata before claiming broad coverage;
   retain explicit resource limits. Read-only commands outside SELECT/set
   operations need a separately defined contract and dialect-specific handling.

3. **P1 — SELECT validation does not establish read-only behavior.**
   `MCP/generator.py:102` rejects statement classes but does not classify function
   effects. An isolated, nonexecuting probe confirmed that the current guard
   accepts `SELECT public.side_effect(id) FROM public.orders` against locally
   constructed table metadata, although no such function appears in that metadata.
   It also accepts `SELECT pg_sleep(30) FROM public.orders`. This contradicts the
   prompt's prohibition on user-defined routine calls. No database function or
   generated SQL was executed. Define a supported function policy per dialect,
   fail closed on unclassified calls where appropriate, and keep downstream
   authorization, routine permissions and execution limits mandatory. Function
   names or SELECT syntax alone cannot prove absence of effects or acceptable cost.

4. **P2 — The request budget is a checkpoint, not a hard deadline.**
   `MCP/generator.py:371` starts multiple metadata operations for each selected
   table without checking the remaining budget between them. Provider calls use
   a fixed 30-second timeout rather than the remaining request time. A request
   can exceed 120 seconds while holding the process-wide lock (line 242), causing
   other calls to fail as busy. Apply remaining-time budgets across operations
   and define bounded admission/concurrency behavior before production load use.

5. **P2 — Container health can report success for an unresponsive server.**
   `compose.yaml:16` checks only for a FIFO, created before server initialization
   by `MCP/server.py`. A blocked live process leaves that file present. Separate
   process liveness from protocol readiness and test unresponsive-process behavior
   without logging protocol payloads or competing with the active stdio client.

## Requirements already satisfied or requiring qualification

- Production uses real SQLAlchemy metadata and ChatDeepSeek by default. Model
  injection and synthetic fixtures are confined to testing pathways; no hardcoded
  business dataset or fake model was found in the default production path.
- Request metadata, selected scope, SQLDatabase and model messages are rebuilt
  per invocation. There is no stored conversation or cross-request scope cache.
  The shared engine, model client, settings and lock are infrastructure state;
  retaining them is compatible with stateless request semantics.
- Changing the configured database requires a restart. The question-only public
  contract does not support client-selected connections per call.
- Generated SQL is not executed; reflection has sample rows disabled. The server
  cannot certify query compilation, business meaning or execution cost under
  those constraints.
- Mocks should remain in isolated tests. Requiring real data and real model
  responses in production does not justify removing deterministic test doubles.

## Initial review validation

Required isolated suite: **67 passed, 26 skipped, one upstream deprecation
warning**, 2.45 seconds. Database/Docker/live-provider integration was not enabled
for this review. Previous verification records are historical evidence, not a
fresh certification. The function probes exercised parsing/validation only.

A second source review checked the findings against the documented boundaries:
statelessness is not reported as broken, SQL execution is not attributed to the
server, and finite resource limits remain necessary. At that initial review the findings remained open. The resolution section above
and verification.md document the subsequent implementation and checks.
