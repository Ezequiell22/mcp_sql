# Contributor instructions

Read README.md, docs/sql-generation-mcp-spec.md and docs/sql-mcp-parser-decision.md before changing behavior. The specification includes target features; README.md and executable schemas describe the supported implementation. Preserve existing user changes.

## Core boundaries

- Expose only `atlas_sql_context_get` and `atlas_sql_finalize`. The host supplies the LLM. Do not add a provider, report lifecycle, frontend, business-row lookup or query execution API to this service.
- PostgreSQL destinations are read only. Product access uses fixed, authorized catalog queries. Never execute generated SQL, PREPARE or EXPLAIN to validate a proposal, and never install helper objects in a destination.
- Treat model output, SQL, descriptions and request text as untrusted. Unknown syntax, dependencies, permissions, functions, types or bindings fail explicitly. Do not weaken the analyzer to accept a failing query.
- Keep credentials outside tool arguments, source control and diagnostics. Local identity comes from the OS; shared identity comes from verified bearer claims. Recheck configured grants and database privileges during finalization.
- Domain models/ports must remain independent of MCP and database drivers. Transport, catalog, parser and credential adapters must not depend on an external reporting application.
- Outputs preserve SQL and exact parameter/filter contracts. Never truncate a query or silently change its business meaning. Distinguish static evidence from unproven semantics, unknown nullability and unassessed cost.

## Verification

From this repository root, install with `python3 -m venv .venv`, `.venv/bin/pip install -r requirements.lock`, and `.venv/bin/pip install --no-deps -e .`.

Run `.venv/bin/pytest` for isolated checks. Database integration tests require Docker and `ALLOW_SYNTHETIC_DB_WRITES=1 .venv/bin/pytest`; they create and remove only a new disposable PostgreSQL container. Never substitute a customer DSN or an existing container. Product code still makes no destination writes.

Check `.venv/bin/python -m atlas_sql_mcp.contracts schemas --check`, documentation links and `git diff --check`. Add focused regressions for behavior changes. Keep public examples, schemas and README.md synchronized. Record only verification actually performed; local tests are not production certification.
