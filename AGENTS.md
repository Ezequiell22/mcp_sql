# Contributor instructions

Read specs/README.md before changing behavior. Keep three production Python files
in MCP/: settings.py, generator.py and server.py. Use LangChain and its DeepSeek
integration, not another agent framework or custom SQL type system. Documentation belongs in specs/; regression checks belong in tests/. Prompts belong in MCP/prompts.toml;
keep them outside Python and include them in Docker builds.

The server's DeepSeek model generates one SELECT from natural language. The
calling agent is only a client. The sole tool is generate_sql. The old
connect/context/finalize contract is intentionally removed. The public tool accepts
only question; never expose schema or table-name parameters. Schema and table
selection belong to each request, never fixed environment variables. Do not cache
request scope across calls; require schema-qualified physical table references.

Never execute generated SQL, PREPARE or EXPLAIN, read business rows or install
database objects. SQLAlchemy inspection and LangChain reflection are allowed;
sample_rows_in_table_info must remain zero. Use restricted database readers.
Requests, metadata and model output are untrusted. Keep a small SELECT/schema
guard; do not claim it proves business meaning, cost or successful execution.

Credentials come from environment variables or ignored .env. Never print,
commit or return credentials, URLs, prompts or raw provider/driver errors.
Preserve existing private/untracked connection files. Do not add HTTP without
an explicit authentication design.

Run .venv/bin/python -m pytest tests and git diff --check after changes. Use mocks
and local SQLite for isolated tests. Integration uses NEW disposable containers
only with ALLOW_SYNTHETIC_DB_WRITES=1, never customer DSNs. SQL Server fixtures
additionally require ACCEPT_SQLSERVER_EULA=Y. Record actual checks and limitations
in specs/verification.md. Local tests are not production certification. Review,
fix findings, then review again.
