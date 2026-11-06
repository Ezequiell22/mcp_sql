# Usage verification — November 6, 2026

## Production hardening and question-only follow-up — final evidence

The production entry point and generator now accept only question. There are no
legacy schema/table-list arguments or associated user validation messages, and no
fixed selected-table count cap. The connection's actual default schema is exposed
as default_schema and can fill missing physical qualifiers only for inspected
objects. PostgreSQL public/custom reader defaults, SQL Server dbo, MySQL database
defaults, CTE aliases and a 25-table selection are covered by regressions.

Final isolated check:

```sh
.venv/bin/python -m pytest tests
```

**108 passed, 35 skipped, one warning**, 7.35 seconds. Skips are gated integration
and live-provider cases. Host Python is 3.14; the production image uses Python 3.12.

Final database/Docker suite on the rebuilt candidate:

```sh
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y MCP_TEST_IMAGE=atlas-sql-mcp:production-review .venv/bin/python -m pytest tests -q --tb=short
```

**138 passed, 5 skipped, one warning**, 148.47 seconds. The five skips require
billed DeepSeek calls. This run includes the final IPC timeout watchdog and partial
reply regression, PostgreSQL 17/18, MySQL 8.4, SQL Server 2022/2025, native/custom
schema defaults, views, materialized views, cross-schema foreign keys, real MCP
stdio, reconnects, secret/protocol exclusion from logs and a suspended-listener
healthcheck. Database fixtures are NEW disposable containers and are removed.

Full real-provider matrix:

```sh
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y RUN_LIVE_DEEPSEEK=1 MCP_TEST_IMAGE=atlas-sql-mcp:production-review .venv/bin/python -m pytest tests -q --tb=short
```

**142 passed, no skips, one warning**, 316.06 seconds. This matrix was completed
immediately before the final IPC watchdog refinement, which added one regression;
it is not represented as a complete rerun after that refinement. Query generation,
dialect mapping, defaulting and metadata behavior did not change afterward.

The final worker was additionally exercised with the real provider:

```sh
ALLOW_SYNTHETIC_DB_WRITES=1 RUN_LIVE_DEEPSEEK=1 .venv/bin/python -m pytest tests/test_mysql.py -q --tb=short
```

**3 passed, one warning**, 12.58 seconds. Includes verified TLS, hostname mismatch,
read-only metadata sessions, views, cross-schema generation and genuine DeepSeek
output through the real stdio tool with the final worker. No returned SQL was
executed. Provider configuration is read privately; no customer DSN is used.

The initial MySQL integration exposed CA-option merging in SQLAlchemy/PyMySQL;
the production connection now uses a single verifying SSLContext. Subsequent
fixture failures were corrected by adding proper certificate key identifiers and
SHOW VIEW grants, without weakening certificate checks. The review also found
SQLGlot's string error-level setting, executable comment semantics, query hints
and partial-IPC timeout coverage; these have fixes and rejection regressions.

The final local image atlas-sql-mcp:production-review was rebuilt successfully.
Its settings.py, generator.py, server.py and prompts.toml hashes match the final
workspace. It contains exactly three production Python files. The existing ERP
MCP container and private connection files were not replaced or changed.

Additional checks passed: git diff --check, docker compose config --quiet and
.venv/bin/python -m pip check. The remaining warning is the upstream
langchain-community sunset notice; the requested LangChain SQL components are
retained. Optional LangSmith tracing is disabled in the production server.

The implementation was reviewed, findings fixed, and reviewed again. See
production.md and tech-lead-review.md for the resulting contract. No additional
blocking finding was identified within that contract. This does not certify an
unknown target's reader grants, CA trust, availability, throughput, business
semantics, SQL compilation/execution or support for every database/read-only
statement. Those are explicit deployment limits, not results of local tests.

## Initial tech lead review baseline (before fixes)

The initial review in [tech-lead-review.md](tech-lead-review.md) found
gaps against the requested database-agnostic/general read-only SQL contract.
Its isolated run completed with **67 passed, 26 skipped, one warning** in 2.45
seconds. Nonexecuting local metadata probes confirmed acceptance of unclassified
function calls by the SELECT guard. No live provider, customer database, Docker
integration or generated SQL execution was used in this review. Runtime code was
not changed in that initial review; the implementation follow-up is documented
in production.md and the resolution section of tech-lead-review.md. `git diff --check` passed after adding
the review and this verification entry.

## Historical ERP deployment before this follow-up

The earlier deployed MCP used the existing Docker iDempiere ERP database through its existing
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

## Historical regression checks before production hardening

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

## Historical existing ERP live smoke checks

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
