# SQL Server adapter and generation quality

> Update: [optional LangChain generation](langchain-generation.md) supersedes the
> original host-only/two-tool restriction when `ATLAS_SQL_MODEL` is configured.
> Catalog-only database access and deterministic validation remain unchanged.

Status: **experimental static subset**, added 26 October 2026. PostgreSQL 18 remains the destination with automated native integration evidence. SQL Server 2022/2025 (major 16/17) are accepted metadata targets, not a claim of native-engine certification. Do not promote this adapter to production until the release gates below pass.

## Operator setup

Install the optional pinned adapter dependencies and [Microsoft ODBC Driver 18](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server), including its operating-system dependencies:

```sh
.venv/bin/pip install -r requirements-sqlserver.lock
.venv/bin/pip install --no-deps -e .
```

A local wheel installation can use `pip install '/absolute/path/atlas_sql_mcp-0.1.0-py3-none-any.whl[sqlserver]'`. The default PostgreSQL installation does not import or require pyodbc. Use [the SQL Server connection example](../examples/connections-sqlserver.json); store the populated configuration outside source control. Set the password through its named environment variable. The same server can route PostgreSQL and SQL Server connections from one configuration file; the operator's `dialect` selects the adapter. Other dialects fail explicitly.

SQL Server requires `sslmode: "verify-full"`. The adapter uses `Encrypt=yes`, `TrustServerCertificate=no` and `ApplicationIntent=ReadOnly`. Application intent is a routing hint, not an authorization boundary: provision a restricted database reader. Elevated, unknown or unavailable sysadmin/db_owner/CONTROL checks fail closed. Certificate verification follows [Microsoft's ODBC guidance](https://learn.microsoft.com/en-us/sql/connect/odbc/major-version-differences). No TLS bypass option is exposed by the product.

Only fixed `sys.*` metadata SELECTs reach the driver. Configured column grants are intersected with `HAS_PERMS_BY_NAME` results; hidden relationships are filtered. SQL Server's [metadata visibility rules](https://learn.microsoft.com/en-us/sql/relational-databases/security/metadata-visibility-configuration) do not replace per-column disclosure checks. The adapter rejects views, computed/masked/encrypted/hidden/custom-type columns and temporal/ledger/external/file tables. It reads trusted, enabled, unfiltered unique keys for fan-out proofs. It does not execute generated SQL, PREPARE, EXPLAIN, sp_describe_first_result_set, stored procedures or helper installation. Policy is reloaded and metadata rechecked at finalization.

## Wire contract

PostgreSQL retains `contractVersion: "1.0"`, `postgresType`, `postgresMajorVersion`, and explicitly cast `$1` parameters. SQL Server uses **`contractVersion: "1.1"`**, `sqlType`, `databaseMajorVersion`, and **`CAST(@p1 AS int)`** style parameters. Positions are contiguous 1–1000; every occurrence must constrain a WHERE/HAVING/JOIN comparison. Consumers bind native parameters rather than interpolate strings. With pyodbc, a consumer must map named parameters to its `?` positional binding API using a proper parser; this service does not execute or rewrite SQL.

There are still exactly two MCP tools. If the client does not know the configured dialect, a 1.0 context request returns a safe `INVALID_CONTRACT` error identifying the required version. Successful context declares dialect-specific capabilities. Use [the SQL Server finalization example](../examples/finalize-sqlserver.json) after fetching its real context. The original SQL is preserved byte for byte in the artifact.

The artifact requires `QUOTED_IDENTIFIER`, `ANSI_NULLS`, `ANSI_WARNINGS`, and `ARITHABORT` to be `ON` in any future consumer execution session. These settings are declarations; the MCP does not run them. Consumer authorization, context freshness and execution limits still apply.

## Tested static subset

- Schema-qualified authorized base tables, explicit column/alias projections, CTEs, derived tables and explicit JOIN ON. Unique-key paths guard aggregate fan-out.
- Numeric SUM/AVG, COUNT/COUNT_BIG, MIN/MAX, conditional CASE/COALESCE, GROUP BY/HAVING, direct-column ranking windows, and positive literal TOP with ORDER BY.
- Exact integer types, bit, date, datetime2/datetimeoffset with precision, bounded char/varchar/nchar/nvarchar, uniqueidentifier and decimal precision/scale. Defaults are checked for bounds, lengths, timezone and precision. Non-Unicode text defaults/literals require ASCII to avoid guessing a codepage.
- Guarded numeric division, supported exact decimal arithmetic, range endpoints and complete output/filter contracts. SQL Server's [SUM](https://learn.microsoft.com/en-us/sql/t-sql/functions/sum-transact-sql) and [AVG](https://learn.microsoft.com/en-us/sql/t-sql/functions/avg-transact-sql) rules are separate from PostgreSQL: smallint/int SUM and AVG return int, COUNT returns int, COUNT_BIG returns bigint, and decimal aggregates retain the specified scale rules.

Batches, writes, hints/options, cross-database references, wildcards, unknown functions/types, implicit incompatible conversions, nullable filters, set operations, recursion, aggregate windows, and decimal arithmetic requiring scale reduction fail explicitly. CASE/COALESCE branches that need unsupported coercion require explicit common casts. Ranking calculations must be precomputed in a CTE. TOP requires an order but does not prove a unique tie-breaker. The parser is conservative and can reject safe SQL outside this subset.

The process-isolated analyzer has the same SQL/AST/deadline budgets as PostgreSQL. SQLGlot is **not SQL Server's native compiler**. Therefore SQL Server artifacts report `syntax: "passed_static_subset"`; they do not claim native syntax verification, business equivalence, execution success or acceptable cost. Unknown output nullability remains null. Descriptions, natural-language requests and SQL are untrusted data.

## Generation and repair loop

The MCP host supplies the model. An internal model provider or agent loop is not necessary to return one query and would not establish correctness. The published server instructions define this host workflow:

1. Retrieve authorized context and all relevant pages; use the connection's dialect and capabilities.
2. Resolve business ambiguity: measure definitions, counted entity, row grain, null policy, currency and time boundaries. A column name or comment is not proof.
3. Generate exactly one complete SELECT and its typed filters/output declarations. Preaggregate one-to-many sources before combining measures. Prefer sargable range predicates where they preserve meaning; never promise an index will be used.
4. Finalize. Feed a structured validation failure into a repair attempt, keeping the original request and meaning. Refresh stale/expired context. Do not weaken policy or replace an unsupported business requirement silently.
5. Stop after three attempts, a repeated failure or unresolved ambiguity. Present a precise limitation instead of labelling failed SQL as validated. On success, return one SQL artifact with its assumptions and evidence.

This is a host orchestration contract, **not a server-enforced generation-quality guarantee**. The server independently enforces its validation rules on every call; it cannot force a third-party model to follow an orchestration prompt. Python source validation belongs to repository verification; accepting or executing model-generated Python is outside this SQL MCP.

## Verification and remaining gates

```sh
# Python syntax, local documentation links, exported schemas, isolated regressions
.venv/bin/python scripts/verify.py

# Offline SQL syntax only, isolated and bounded; no connection or query execution
.venv/bin/atlas-sql-check --dialect sqlserver /absolute/path/query.sql
.venv/bin/atlas-sql-check --dialect postgresql /absolute/path/query.sql

# Native PostgreSQL fixture: always creates/removes its own disposable container
ALLOW_SYNTHETIC_DB_WRITES=1 .venv/bin/pytest

# Native SQL Server aggregate type/value checks: separate explicit EULA acceptance
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y \
  .venv/bin/pytest tests/test_sqlserver_engine.py
```

The syntax command returns `parsed`, not a validated artifact. It checks neither authorization nor read-only policy; a parsable write statement can pass that diagnostic. T-SQL evidence is `non_native_parser_only`. Actual MCP finalization remains mandatory.

The SQL Server native fixture creates a new container without host ports, mounts or access to a customer DSN, and removes it afterward. Its sqlcmd certificate bypass is confined to that disposable fixture. It uses Microsoft's `2022-latest` test image; record/pin the image digest when publishing reproducible certification results. It does not test the product's verified-TLS connection path. Native SQL Server tests have **not been run in this change**. Final local verification: **401 passed, 6 skipped**, including the 87 disposable PostgreSQL checks. Python syntax, formatting/lint, schema consistency, local documentation links, wheel packaging and installed-wheel isolated analysis for both dialects passed.

Before production: run and expand native conformance for both accepted majors, verify the actual ODBC catalog path with trusted TLS and restricted roles, reconcile joins/windows/conditional measures against independent business fixtures, exercise deadlines under load, and evaluate real host models against a versioned request/reference corpus. Add other dialects only with dedicated metadata/type/authorization adapters and evidence; parser dialect availability alone is insufficient. No static parser or agent loop proves efficient plans or natural-language equivalence.
