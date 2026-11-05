# MCP SQL — Atlas

Connect an AI agent to a database using a connection string. The agent reasons
about the request and writes SQL; this MCP discovers live metadata and validates
that SQL. Supports PostgreSQL 18 and SQL Server 2022/2025 within the published SQL subsets.
The connected agent discovers the database's actual structure; no customer
catalog, sample data, table mappings or reporting application is required.

The MCP returns complete SQL, typed filters, output metadata and validation evidence.
It reads database metadata only. It does not execute reports or retrieve business records.

## Use it

After one-time administrator setup, provide your restricted-reader connection string
and ask for the report in chat:

> Connect using my connection string. Create SQL for monthly revenue by region,
> excluding canceled invoices, with date filters.

The assistant handles connections, metadata discovery, grouping, filters and validation.
It asks about missing business definitions. You do not need JSON or a connection file.
The chat client may retain the string; use your organization's approved credential policy.
Connections expire after an hour or a restart; reconnect when prompted.

## Install once

Docker is the packaged deployment path and includes both database drivers:

```sh
ACCEPT_EULA=Y docker compose up -d --build
```

This accepts Microsoft's ODBC driver license and starts the persistent local container. Register the
[Docker client template](configs/codex-docker.toml) using the [Docker guide](docs/docker.md).
For local Python installation, other clients, environment settings or shared HTTP,
follow [configuration](docs/configuration.md) and the [client templates](configs/README.md).

**Where is the LLM?** It runs in your connected agent, such as Codex, Claude Code
or Cursor. The MCP has no LLM, provider configuration or model API key. It supplies
live metadata and validates the agent's SQL. See [architecture and agent decisions](docs/architecture.md).

## What to expect

- Real metadata discovery, restricted-reader authorization and typed parameters.
- Dialect-specific static validation with explicit failures for unsupported SQL.
- Complete SQL without automatic result-row caps, sampling or query truncation.
- Joins, grouping, filters, CTEs and analytical constructs within each supported subset.

PostgreSQL and SQL Server require separate adapters; other engines are unsupported.
Both SQL Server versions have native-engine and verified-TLS acceptance evidence.
Unknown business rules require clarification. Static validation does not prove business
correctness, runtime success or efficient query plans. Read [limits and production
acceptance](docs/operations.md) before rollout.

## Verify and integrate

Install the development environment before running verification:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python scripts/verify.py
```

Database tests require explicit disposable-fixture flags; see [test scenarios,
reproduction commands and recorded evidence](docs/testing.md). Tests run without
those flags skip database checks. Simulated agents are not live-model evaluations.

Use [contract 1.2](docs/neutral-contract.md) for database-neutral fields. See the
[tool/artifact contract](docs/contract.md), [tool payload examples](examples/README.md),
and [documentation index](docs/README.md). Runtime source is in `src/atlas_sql_mcp`;
fixtures and reporting records are confined to `tests`.
