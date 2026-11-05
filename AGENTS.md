# Contributor instructions

Read README.md, docs/architecture.md and docs/configuration.md before changing behavior. These guides and executable schemas describe the supported implementation. Preserve existing user changes.

## Core boundaries

- Keep `atlas_sql_context_get` and `atlas_sql_finalize` compatible. The connected agent is the only LLM: the server exposes no model provider, provider credentials or generation tool. Expose `atlas_sql_connect` for user-supplied connection strings without a required configuration file. Reuse the deterministic validator; do not add report lifecycle, frontend, business-row lookup or query execution APIs.
- PostgreSQL destinations are read only. Product access uses fixed, authorized catalog queries. Never execute generated SQL, PREPARE or EXPLAIN to validate a proposal, and never install helper objects in a destination.
- Treat model output, SQL, descriptions and request text as untrusted. Unknown syntax, dependencies, permissions, functions, types or bindings fail explicitly. Do not weaken the analyzer to accept a failing query.
- `atlas_sql_connect` accepts caller-provided credentials in a connectionString, bound to an internal principal and retained only in expiring server memory. Never echo/persist/log credentials or pass them to generation prompts. Shared deployments require explicit allowed database endpoints. Keep credentials outside source control and diagnostics. Local identity comes from the OS; HTTP has no built-in authentication and uses the common internal identity `shared#anonymous`; external infrastructure owns access control. Recheck configured grants and database privileges during finalization.
- Domain models/ports must remain independent of MCP and database drivers. Transport, catalog, parser and credential adapters must not depend on an external reporting application.
- Outputs preserve SQL and exact parameter/filter contracts. Never truncate a query or silently change its business meaning. Distinguish static evidence from unproven semantics, unknown nullability and unassessed cost.

## Verification

From this repository root, install with `python3 -m venv .venv`, `.venv/bin/pip install -r requirements-dev.lock`, and `.venv/bin/pip install --no-deps -e .`.

The base requirements.lock is runtime-only; tests/lint require requirements-dev.lock. Run `.venv/bin/pytest` for isolated checks. Database integration tests require Docker and `ALLOW_SYNTHETIC_DB_WRITES=1 .venv/bin/pytest`; PostgreSQL fixtures create and remove new disposable containers. Native SQL Server fixtures additionally require explicit `ACCEPT_SQLSERVER_EULA=Y`; full Docker workflows require `ATLAS_SQL_DOCKER_IMAGE` pointing to the verified local image. Run `scripts/verify.py --integration` with all three gates for the zero-skipped acceptance check. See docs/testing.md for commands and license gates. Never substitute a customer DSN or an existing container. Product code still makes no destination writes.

Check `.venv/bin/python -m atlas_sql_mcp.contracts schemas --check`, documentation links and `git diff --check`. Add focused regressions for behavior changes. Keep schemas, test fixtures and README.md synchronized. Record only verification actually performed; local tests are not production certification.
