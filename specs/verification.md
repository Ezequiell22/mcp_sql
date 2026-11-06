# Usage verification — November 6, 2026

## Latest tech lead review

The subsequent review in [tech-lead-review.md](tech-lead-review.md) found open
gaps against the requested database-agnostic/general read-only SQL contract.
Its isolated run completed with **67 passed, 26 skipped, one warning** in 2.45
seconds. Nonexecuting local metadata probes confirmed acceptance of unclassified
function calls by the SELECT guard. No live provider, customer database, Docker
integration or generated SQL execution was used in this review. Runtime code was
not changed; the findings remain open. `git diff --check` passed after adding
the review and this verification entry.

## Current runtime and configuration

The MCP now uses the existing Docker iDempiere ERP database through its existing
restricted reader, verified PostgreSQL TLS and read-only CA mount. Metadata-only
inspection from the deployed container found **912 base tables**. The reader's
superuser, create-database, create-role, replication and bypass-RLS flags were all
false. This is not a complete privilege audit or production certification.

The repository has exactly one root `.env`, private and ignored. Settings and
Compose use it directly; provider integration tests read only its provider
configuration and never its DATABASE_URL. Alternate runtime env files and demo
selectors were removed. Existing private connection files and the ERP project's
own configuration were preserved. The task-created synthetic runtime database
was stopped; the existing ERP PostgreSQL and SQL Server containers remain running.

Exactly one canonical MCP container, `atlas-sql-mcp`, runs one Python server.
Inspector forwards stdio through the exclusive-lock bridge to that process.
No HTTP MCP endpoint was introduced. The image includes external prompts and
exactly three production Python files. All request state and metadata are rebuilt
for each invocation. No business rows or generated SQL were executed.

## Regression checks

Required isolated run:

```sh
.venv/bin/python -m pytest tests -q
```

**67 passed, 26 skipped.** Uses mocks and local SQLite; gated Docker/live-provider
cases skip. One upstream langchain-community deprecation warning remains.

Database and Docker integration, without live provider billing:

```sh
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y MCP_TEST_IMAGE=atlas-sql-mcp:local .venv/bin/python -m pytest tests -q
```

The final follow-up run passed **89 tests, with 4 skipped**, in 58.46 seconds,
including the bounded selection-repair regression. Integration databases are NEW disposable PostgreSQL 17/18 and
SQL Server 2022/2025 containers; fixture cleanup removes them. No customer DSNs
are used for fixture creation. The live-provider matrix was not rerun in full
for this follow-up; earlier live results are not treated as ERP certification.

New checks cover structured clarification rather than Tool Error, no-match versus
ambiguous source decisions, single .env loading, and rejection/repair of invented
batch identifiers. A ChatDeepSeek HTTP transport regression captures three
consecutive requests, verifies three fresh metadata scans, no prior question in
subsequent payloads and identical first/final repeated-question payloads after an
intervening different question. This is evidence of request isolation, not a
production load/concurrency certification.

## Existing ERP live smoke checks

Actual stdio MCP calls use only question; tool discovery exposes only generate_sql
with exactly that field. Concrete questions counting application clients and
listing up to ten active application clients by name generated schema-qualified
SELECT output with execution_performed=false. No returned SQL was executed.

The exact reported Portuguese 360-degree company overview question was rerun.
Initial attempts exposed invalid metadata selections inside partial catalog
batches. The server rejects those identifiers, permits one bounded corrective
selection with exact candidate feedback, then still fails safely if invalid.
The selection prompt now explicitly declines an undefined comprehensive overview
rather than inventing its fields or metrics. The final repeated question returned
status=needs_clarification, reason=metadata_no_match, execution_performed=false,
without an MCP Tool Error or SQL. Inspector was reconnected to the ERP MCP; its
form showed only Question, and the client-count request completed with OK in
8879 ms. A clarification must be resubmitted as part of
a new complete question because MCP stores no conversation.

The application distinguishes clarification from infrastructure/guard failures.
Clarifications log INFO events with safe reason codes. Invalid model identifiers
remain errors; they are never silently accepted or rewritten. The bounded
semantic correction is distinct from provider retries, which remain disabled.

## Logs, review and limitations

Structured Docker diagnostics cover readiness, receipt, metadata counts, selection,
reflection, generation, validation, completion, clarification and failures. IDs
correlate requests; no questions, SQL, prompts, credentials or raw driver/provider
errors are logged. Protocol stdout remains in private local pipes. Docker log
rotation remains 10 MB with three retained files. Regression checks cover
reconnects, a stable server process, concurrent-client rejection and secret/protocol
exclusion. See inspector.md for steps and review.md for review findings.

Reviewed, fixed findings and reviewed again. SELECT/schema guards, fully qualified
physical references, sample_rows_in_table_info=0 and metadata-only access retained.
There is no fixed table allowlist; documented catalog/context resource budgets and
one-schema-per-query support remain. Views/cross-schema joins are outside the
current contract. SQL business meaning, successful compilation/execution, cost,
production authorization and provider availability are not certified by these
checks. No synthetic records were inserted into the existing ERP.

Final checks: required isolated suite 67 passed/26 skipped, git diff --check passed;
deployed settings.py, generator.py, server.py and prompts.toml hashes match the
workspace. Docker logs contained none of the configured URL/key, checked questions,
returned SQL or JSON-RPC protocol. The final prompt wording was also exercised by
the existing-ERP live smoke checks; the entire billed provider matrix was not rerun.
