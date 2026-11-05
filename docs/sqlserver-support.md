# SQL Server adapter and agent SQL validation

> [Connection strings](connection-strings.md) are now supported by `atlas_sql_connect`.
> This supersedes the original prohibition on caller-provided credentials and
> removes the requirement for a preconfigured connection file.

Status: **supported static subset for SQL Server 2022 and 2025**. Both versions
have native type/value checks, real MCP/ODBC verified-TLS workflows and shared
HTTPS authentication evidence. SQLGlot is not SQL Server's native compiler;
validation applies to the published subset and does not certify arbitrary SQL,
business equivalence or execution cost. See [recorded evidence](testing.md).

## Operator setup

Install the optional pinned adapter dependencies and [Microsoft ODBC Driver 18](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server), including its operating-system dependencies:

```sh
.venv/bin/pip install -r requirements-sqlserver.lock
.venv/bin/pip install --no-deps -e .
```

A local wheel installation can use `pip install '/absolute/path/atlas_sql_mcp-0.1.0-py3-none-any.whl[sqlserver]'`. The default PostgreSQL installation does not import or require pyodbc. For the default user workflow, start the MCP without a configuration file and paste a SQL Server URI or `Server=...;Database=...;UID=...;PWD=...` string into the chat; see [accepted strings](connection-strings.md). The assistant handles the connection ID. Alternatively, administrators can use [the SQL Server connection example](../configs/connections.example.json), store the populated configuration outside source control and set the password through its named environment variable. The same server can route PostgreSQL and SQL Server connections from one configuration file; the operator's `dialect` selects the adapter. Other dialects fail explicitly.

SQL Server requires `sslmode: "verify-full"`. The adapter uses `Encrypt=yes`, `TrustServerCertificate=no` and `ApplicationIntent=ReadOnly`. Application intent is a routing hint, not an authorization boundary: provision a restricted database reader. Elevated, unknown or unavailable sysadmin/db_owner/CONTROL checks fail closed. Certificate verification follows [Microsoft's ODBC guidance](https://learn.microsoft.com/en-us/sql/connect/odbc/major-version-differences). No TLS bypass option is exposed by the product.

Only fixed `sys.*` metadata SELECTs reach the driver. Configured column grants are intersected with `HAS_PERMS_BY_NAME` results; hidden relationships are filtered. SQL Server's [metadata visibility rules](https://learn.microsoft.com/en-us/sql/relational-databases/security/metadata-visibility-configuration) do not replace per-column disclosure checks. The adapter rejects views, computed/masked/encrypted/hidden/custom-type columns and temporal/ledger/external/file tables. It reads trusted, enabled, unfiltered unique keys for fan-out proofs. It does not execute generated SQL, PREPARE, EXPLAIN, sp_describe_first_result_set, stored procedures or helper installation. Policy is reloaded and metadata rechecked at finalization.

## Wire contract

PostgreSQL retains `contractVersion: "1.0"`, `postgresType`, `postgresMajorVersion`, and explicitly cast `$1` parameters. SQL Server uses **`contractVersion: "1.1"`**, `sqlType`, `databaseMajorVersion`, and **`CAST(@p1 AS int)`** style parameters. Positions are contiguous 1–1000; every occurrence must constrain a WHERE/HAVING/JOIN comparison. Consumers bind native parameters rather than interpolate strings. With pyodbc, a consumer must map named parameters to its `?` positional binding API using a proper parser; this service does not execute or rewrite SQL.

`atlas_sql_connect` creates a user-bound connection from a string; `atlas_sql_context_get` and `atlas_sql_finalize` discover and validate SQL. The connected agent writes SQL; the MCP never calls an LLM. If the client does not know the configured dialect, a 1.0 context request returns a safe `INVALID_CONTRACT` error identifying the required version. Successful context declares dialect-specific capabilities. Use [the SQL Server finalization example](../examples/finalize-sqlserver.json) after fetching its real context. The original SQL is preserved byte for byte in the artifact.

The artifact requires `QUOTED_IDENTIFIER`, `ANSI_NULLS`, `ANSI_WARNINGS`, and `ARITHABORT` to be `ON` in any future consumer execution session. These settings are declarations; the MCP does not run them. Consumer authorization, context freshness and execution limits still apply.

## Tested static subset

- Schema-qualified authorized base tables, explicit column/alias projections, CTEs, derived tables and explicit JOIN ON. Unique-key paths guard aggregate fan-out.
- Numeric SUM/AVG, COUNT/COUNT_BIG, MIN/MAX, conditional CASE/COALESCE, GROUP BY/HAVING, direct-column ranking windows, and positive literal TOP with ORDER BY.
- Exact integer types, bit, date, legacy datetime/smalldatetime, datetime2/datetimeoffset with precision, bounded char/varchar/nchar/nvarchar, varchar(max)/nvarchar(max), uniqueidentifier and decimal precision/scale. Defaults are checked for bounds, lengths, timezone and precision. Non-Unicode text defaults/literals require ASCII to avoid guessing a codepage.
- Legacy datetime defaults require exact whole seconds on or after 1753-01-01; smalldatetime defaults require exact minutes from 1900-01-01 through 2079-06-06 at 23:59. Both exclude timezone offsets. These conservative default checks avoid silent rounding; they do not restrict existing stored values to whole seconds. Text declared MAX remains subject to MCP input/payload budgets. Money and smallmoney remain unsupported; their arithmetic/coercion rules are not inferred from decimal support.
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

The SQL Server native fixture creates a new container without host ports, mounts
or access to a customer DSN, and removes it afterward. Its sqlcmd certificate
bypass is confined to that disposable fixture. It uses Microsoft's `2022-latest`
test image; record the resolved image digest with release evidence. The tests
compare actual engine output with analyzed aggregate types/values, legacy timestamp
types and integer arithmetic precedence. They use explicit loopback TCP with
bounded login/query timeouts. The test-only `ATLAS_SQL_SQLSERVER_IMAGE` override selects 2022 or 2025. The
product's ODBC/verified-TLS catalog path is verified in separate workflows. See [current recorded
results](testing.md) for the verified checkout, counts and commands.

For a customer deployment, validate its certificates and permissions, reconcile its business definitions with reference results, measure its expected load, and evaluate the chosen host model. Add other dialects only with dedicated metadata/type/authorization adapters and evidence; parser dialect availability alone is insufficient. No static parser or agent loop proves efficient plans or natural-language equivalence.


The [containerized SQL Server workflow suite](testing.md#sql-server-inside-docker)
exercises the actual MCP image, ODBC Driver 18, SQL Server 2022, verified TLS,
restricted-reader discovery, CTEs, ranking, grouping and typed date filters.
Untrusted certificates and unsafe proposals must fail. This closes the original
local verified-TLS workflow gap for SQL Server 2022; deployment trust, identity
and load acceptance still belong to the chosen production environment.
