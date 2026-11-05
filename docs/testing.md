# Reporting workflow tests and release evidence

The tests simulate a host agent using the actual MCP protocol. The agent script
connects with a connection string, reads real database metadata, submits a complete
SQL/filter proposal and receives validation. An independent test harness then
executes accepted SQL against synthetic records and compares the result with
explicit expectations. The MCP never executes the report query.

These are deterministic agent simulations, not live LLM quality measurements.
They deliberately specify the meaning of revenue, posted invoices and margins.
Passing them cannot establish that a model will infer an undocumented business rule.

## PostgreSQL ERP scenarios

`tests/test_erp_workflows.py` starts a new PostgreSQL 18 container with parties,
invoices, invoice lines and payments. The MCP runs as an actual stdio subprocess,
with no preconfigured customer schema or catalog file.

| Scenario | What is verified |
| --- | --- |
| Regional sales | Multiple joins, posted-invoice rule, quantities and prices, grouping |
| Product margin | Exact monetary arithmetic and product grouping |
| Accounts receivable | Separate line/payment CTE aggregates prevent join duplication |
| Selected date period | Typed from/until filters with inclusive/exclusive endpoints |
| Customer ranking | Aggregation and ranking window |
| Customers without invoices | Anti-join logic and empty activity |
| Five-year monthly dashboard | More than 1,000 SQL lines, 60 monthly CTEs, complete SQL preservation and reconciled totals |
| Invalid proposals | Invented column/table, writes and multiple statements produce no artifact |

The expected report results are written independently of the SQL under test.
`tests/test_postgres.py` additionally checks native output types, authorization
drift, arbitrary newly named schemas and the larger analytical reference corpus.

## Result volume

`tests/test_report_volume.py` populates a new disposable PostgreSQL database with
one million synthetic events. The agent submits a complete unbounded report SQL;
finalization must preserve it exactly. Only the independent harness executes it
and verifies one million result rows. A separate explicit top-ten request retains
its LIMIT 10. Schema-discovery pagination does not cap report result rows.

## SQL Server inside Docker

`tests/test_sqlserver_live.py` creates a new internal Docker network, short-lived
TLS certificate and SQL Server 2022 database with actual synthetic invoice rows.
The production MCP image runs non-root with a read-only root filesystem. Its
ODBC driver verifies the server certificate; no certificate bypass reaches the MCP.
Only test setup uses the SQL Server administrator account. The MCP uses a separate
restricted reader.

| Scenario | What is verified |
| --- | --- |
| Regional invoice totals | Real connection string → discovery → validation → independent native result |
| Preaggregated balances | CTE and join type resolution |
| Invoice ranking | Ordered ranking window and derived-table filtering |
| Date-filtered totals | Neutral contract and typed SQL Server parameter bindings |
| Untrusted certificate | Connection fails instead of bypassing verification |
| Unsafe agent proposals | Invalid credentials, writes, invented columns, multiple statements and system-table references fail |

`tests/test_sqlserver_engine.py` separately reconciles six native aggregate
return types and values. Both SQL Server 2022 and 2025 are covered by the test-only image override described below.

## Reproduce

Create the Python environment as described in [configuration](configuration.md),
then install the development lock for the complete
regression suite:

```sh
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
```

`requirements.lock` contains runtime dependencies only; it does not install pytest
or Ruff. SQL Server Python/ODBC dependencies used by the live workflow are packaged
inside the MCP Docker image. Docker must be running. The following commands accept Microsoft's driver/server EULAs;
run them only when authorized to do so:

```sh
docker build --build-arg ACCEPT_EULA=Y -t atlas-sql-mcp:local .
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y \
  ATLAS_SQL_DOCKER_IMAGE=atlas-sql-mcp:local \
  .venv/bin/python scripts/verify.py --integration
```

The integration tests create their own resources, never accept a customer DSN,
and remove their database containers and networks in fixture cleanup. Generated
TLS files are test-only temporary files. No test code or data is packaged into
the runtime image. Tests without explicit integration settings skip the relevant
fixtures; a skipped test is not evidence of a passing database workflow.

The CI workflow includes an opt-in Docker/SQL Server matrix for 2022 and 2025. Set the repository
variable `ACCEPT_SQLSERVER_EULA=Y` after the repository owner accepts the relevant
licenses. Each job builds the image and runs the workflows and native SQL
Server checks. A local passing run does not establish a passing CI run.

## Shared deployment and concurrent callers

`tests/test_shared_deployment.py` exercises an actual Docker HTTPS ingress and JWKS
issuer, an authenticated MCP SDK client, restricted-reader SQL Server TLS access,
and cross-identity denial. This verifies the deployed protocol path in its disposable
fixture; it does not validate the customer's issuer configuration or hosted client.

A fixture run with ten concurrent context requests and finalizations passed, with
maximum observed times of **0.0699 seconds** and **0.6906 seconds**, respectively.
These are local fixture observations, not sustained-load percentiles or capacity claims.

## Verification records

Verified on **5 November 2026**: **610 tests passed, zero skipped, in 74.21 seconds**
using the strict integration command above. This includes native PostgreSQL 18,
SQL Server 2022, authenticated HTTP, concurrent requests, large SQL, one million
report rows, scoped large-schema discovery, privilege revocation, inherited reads,
case-sensitive identifiers, typed bindings and rejection cases.

A separate 18-check native/ODBC/HTTPS suite passed on SQL Server 2022 and again
on SQL Server 2025, with no skips. The fixtures query the actual server major
version. To select 2025, set
`ATLAS_SQL_SQLSERVER_IMAGE=mcr.microsoft.com/mssql/server:2025-latest` alongside
the integration gates; omitting it selects 2022. The same matrix is configured
in CI, but these recorded results are local runs, not a claim that remote CI ran.

Lint, formatting, documentation links, exported schemas, JSON/TOML and whitespace
checks passed. A standalone wheel built and was inspected: runtime code only,
with no generation adapter, provider dependency, test fixture or lab credentials.
The Docker runtime contains both database drivers, runs as UID 10001, and exposes
only `atlas_sql_connect`, `atlas_sql_context_get` and `atlas_sql_finalize`.

The [downloaded ERP lab](../labs/erp/README.md) adds separate evidence from genuine,
vendor-populated schemas: **66 inventory pages covering 6,314 tables** across
three databases, plus **18 report finalizations**. All 18 reports also executed
in an independent read-only test harness and reconciled against calculations
from original base rows. That execution is outside the MCP; no product query
execution or business-row API was introduced. These deterministic proposals do
not establish live-model natural-language accuracy or arbitrary ERP compatibility.

The local release image ID is
`sha256:9b13334e7e61642ce7b1af0a36f1f4729b6b43df1d11de06768679f2b4902ff9`.
The persistent `atlas-sql-mcp` container is updated and the three ERP database
containers remain available. This image is local, not a published registry release.

`--integration` requires explicit fixture flags and a Docker image, and fails
if any test is skipped. Without it, `scripts/verify.py` runs ordinary regressions
and may skip database fixtures. `--checks-only` validates syntax, local Markdown
links/anchors, JSON/TOML templates and exported schemas without tests.

## Production boundaries

These tests do not certify live model success rates, every ERP schema or SQL
construct, customer production ingress/identity-provider configuration,
multiple-replica session handling, or latency under sustained concurrent load.
The MCP supports the published dialect/version subsets and rejects unsupported
constructs. An accepted artifact establishes static checks, not business
correctness, runtime success, execution authorization or an efficient query plan.

