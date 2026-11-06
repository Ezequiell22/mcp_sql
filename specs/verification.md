# Verification — SELECT executor

Scope: replace the SQL generation service with one `execute_select(sql)` tool.
The former generation/live-model evidence and tests are obsolete and removed.
This report applies to the executor refactor, not the previously running image.

## Review findings addressed

- Removed the model, prompts, metadata discovery, MySQL support, old environment
  requirements and generation contracts. Production has three Python files and
  seven direct dependency entries.
- Kept the existing uncommitted Docker public-CA installation and Compose secret,
  network, read-only filesystem and persistent stdio configuration. Rewrote the
  affected docs for execution; original local edits were backed up before editing.
  Existing ignored environment and Inspector files were not edited.
- Preserved native SQL exactly. `no_parameters=True` avoids psycopg treating
  literal percent signs as parameter formatting; real-driver tests cover this.
- Rejected nested writes, multiple statements, SELECT INTO, locks, hints, unsafe
  routines, placeholders and external/cross-database table sources. Fixed handling
  of SQLGlot's normalized DATE_TRUNC node and empty SELECT projections during review.
- Used ordered column names and row arrays so duplicate aliases do not discard
  values. Decimal precision, binary data, NULLs and date/time/interval values have
  explicit JSON conversion. Unknown values fail the call without partial results.
- Explicitly disabled autocommit and driver pooling; used fresh connections and
  rollback on each call. Read-only PostgreSQL transactions add a second barrier.
  SQL Server requires actual restricted grants, exercised with a synthetic reader.
- Retained query/login timeouts with QUERY_TIMEOUT_SECONDS, sanitized public
  errors and busy admission. Documented that native driver timeouts do not bound
  fetching/serialization/delivery or guarantee cancellation during network faults.
- Kept the Docker bridge exclusive and protocol output private; tested reconnects
  to the same listener, healthcheck and absence of SQL/credentials/protocol in logs.

## Executed checks

Initial isolated run: **84 passed, 3 skipped** before adding PostgreSQL integration
and the persistent-bridge regression. Initial full integration run: **89 passed**.

**Final full suite: 90 passed, 0 skipped, 0 failures in 34.88 seconds.**
Ruff lint/format, Python compilation, diff whitespace, Compose validation, Docker
build and image dependency consistency all passed. Final diff reviewed after fixes;
no unresolved blocking finding in the implemented scope.

Commands:

```sh
.venv/bin/python -m ruff check MCP tests
.venv/bin/python -m ruff format --check MCP tests
.venv/bin/python -m compileall -q MCP tests
git diff --check
docker compose config --quiet
docker build --build-arg ACCEPT_EULA=Y -t atlas-sql-mcp:select-test .
docker run --rm --entrypoint python atlas-sql-mcp:select-test -m pip check
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y MCP_TEST_IMAGE=atlas-sql-mcp:select-test .venv/bin/python -m pytest tests -q
```

Integration creates only new disposable PostgreSQL 17/18 and SQL Server 2022/2025
containers, with synthetic credentials and a dedicated SQL Server TLS CA. Fixture
administration may create/write synthetic objects; product execution uses only
its restricted reader. The fixture admin uses a trust override solely to bootstrap
the disposable SQL Server; product connections verify its CA and hostname.
Containers are removed in finally blocks. No customer database URL is loaded by
the integration fixtures and no existing runtime container is replaced.

Coverage includes actual MCP discovery and calls, unchanged percent literals,
CTEs/aggregates, explicit LIMIT/TOP, 1500 rows without truncation, empty result
metadata, duplicate columns, JSON conversion, denied database writes, real lock
contention timeouts and successful calls after timeout/error cleanup. The image
is independently installed from the reduced requirements and checked with pip.

## Limits

No production account grants, production data, high-volume memory ceiling or
arbitrary SQL grammar compatibility was certified. The SQL parser accepts a
conservative subset and cannot establish that database-defined objects are free
of side effects. Database authorization is mandatory. There is intentionally no
result-size cap; very large responses can exhaust memory or exceed client limits.
The native query timeout is not a total response deadline. The existing business
deployment was not rebuilt/restarted by this task; its clients must be migrated
from the former question tool to SQL before deploying this breaking contract.
