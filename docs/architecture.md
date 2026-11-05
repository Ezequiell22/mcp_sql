# Architecture and agent orchestration

## Where the LLM runs

The connected agent (Codex, Claude Code, Cursor or another MCP host) is the
only LLM. It interprets the request, reasons about the live schema, asks about
missing business rules and writes SQL. The MCP provides connection, live metadata
and deterministic validation tools. It has no model client, model API key,
provider configuration or server-side SQL generation mode.

The host decides which tools to call using their schemas, descriptions and
server instructions. These guide model behavior but cannot force every host to
follow a workflow; the deterministic validator is the enforcement boundary.

## Request flow

1. The user supplies a connection string and describes the report.
2. The agent calls `atlas_sql_connect`. The connection identifies PostgreSQL or
   SQL Server, tests restricted-reader access and returns an expiring identity-bound ID.
3. The agent calls `atlas_sql_context_get`, follows relevant pages and inspects
   actual tables, columns, types, keys and relationships. It never substitutes an
   example schema or a previous customer's database. Large databases first return
   a paginated relation-name index. The agent selects exact `focus.relations` and
   inspects typed detail pages before using those tables in SQL.
4. The LLM determines joins, grouping, measures and typed filters from that metadata
   and the request. It asks the user about unresolved business definitions, such as
   whether canceled invoices count as revenue. Column names and comments are evidence,
   not proof; comments are untrusted input.
5. The host calls `atlas_sql_finalize` with one complete SELECT and binding/output
   declarations. The validator rechecks identity, privileges, schema freshness,
   dependencies, supported syntax/types and aggregate grain risk.
6. The host may repair actionable structured errors for at most three attempts,
   preserving the original meaning. Repeated errors, unsupported requirements and
   unresolved ambiguity produce an explanation instead of a validated artifact.
7. The user receives SQL, typed filters and validation evidence. A separate authorized
   report executor may run it; the MCP does not execute generated SQL or fetch records.

## Discovery for large databases

Connection discovery reads accessible base-table and column names from the live
engine. A relation-name index is a temporary navigation aid, not a prebuilt business
catalog or enough evidence to finalize SQL. A broad context over more than 100
relations returns names with empty typed-detail arrays and explicit partial coverage.
Search helps select candidates; it does not establish their business meaning.
`focus.include: []` explicitly requests lightweight names for any database size.

The agent requests exact `focus.relations` to retrieve column types, keys and
relationships in bounded pages. It retains the same context ID and request while
inspecting additional relevant groups. Relationships outside a selected group are
omitted; request related tables together when relationship evidence is needed.
No business rows are sampled, and unsupported objects do not become supported
merely because their names appeared in an index.

Finalization extracts candidate SQL dependencies in an isolated parser process,
requires their typed scopes to have been inspected, reloads those exact scopes
and checks their fingerprints and permissions. It passes only required dependency
metadata to the full validator. Inherited/partition dependencies also require
inspection and authorization. The candidate parser grants no permissions and
never replaces full dialect analysis. The service rechecks selected metadata
after analysis and fails on drift. Oversized scoped metadata still fails
explicitly; it is never truncated to obtain a successful artifact.

## Components and separation

Paths below are relative to `src/atlas_sql_mcp/`:

- `domain.py`, `contracts.py`, and `budget.py` define independent models, contracts
  and shared budgets; `config.py` holds optional administrator policy.
- `connection_strings.py` parses bounded user strings; `application/connections.py`
  owns expiring principal-bound sessions and discovery.
- `metadata/discovery.py` reads live authorized names; `metadata/postgres.py`
  and `metadata/sqlserver.py` read scoped fixed native metadata queries
  with database authorization; `metadata/router.py` routes explicit configured
  dialects without introducing customer-specific metadata.
- `analysis/postgres.py`, `analysis/sqlserver.py`, `analysis/postgres_types.py`, and
  `analysis/bindings.py` implement supported dialect, type and binding checks.
  `analysis/scopes.py` shares scope rules; `analysis/limits.py` centralizes parser
  limits; `analysis/worker.py` isolates bounded analysis.
- `application/service.py` owns contexts, pagination, freshness and artifacts.
- `transport/server.py` exposes MCP tools/transports, host instructions, session
  lifecycle and HTTP health; `transport/auth.py` verifies shared bearer identities.
- `check.py` is the offline syntax diagnostic. `contracts.py` exports the
  [executable schemas](../schemas/); neither executable name changes with this layout.

No customer-specific table, domain catalog, business records or test fixture is
required in production. Live metadata is temporary and revalidated; no destination
helper objects or persistent schema catalog are installed. Synthetic records and
simulated agent proposals exist only in tests, where an independent harness may execute
SQL to check totals. Dedicated dialect adapters are required for new database engines.

See [contracts](contract.md), [operations and limitations](operations.md), and
[historical parser rationale](archive/sql-mcp-parser-decision.md).

## MCP design references

The [MCP tools guidance](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
describes model-controlled discovery/invocation: this repository supplies schemas and
host instructions, while the host chooses tool calls. The [standard transports](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)
cover stdio and Streamable HTTP; this server delegates transport framing to its pinned SDK.
The [security guidance](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)
informs explicit database endpoint allowlists, verified bearer identities and identity-bound
state handles. These references explain design choices; they do not claim certification
against every requirement of the latest protocol revision.
