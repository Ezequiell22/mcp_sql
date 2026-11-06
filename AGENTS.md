# Contributor instructions

Read specs/README.md before changing behavior. Keep the service small: settings.py,
executor.py and server.py in MCP/. The sole public tool is execute_select(sql).
Use one fixed DATABASE_URL and SQLAlchemy with PostgreSQL/psycopg or SQL Server/pyodbc.
Do not add LLMs, metadata discovery, SQL generation, automatic row limits or pagination.
Preserve the original SQL, the single-statement SELECT guard, routine restrictions,
rollback, configured query timeout, sanitized errors and verified TLS.

Database permissions are the security boundary. Require a restricted reader with
SELECT only on approved objects and no write, owner/admin or unsafe routine access.
Never use a customer DSN for tests or create database objects at runtime.
Credentials come from environment or ignored .env. Preserve private connection files.
Never log or return credentials, connection URLs, SQL or raw driver errors.
Stdio is the only transport. Preserve the private Docker bridge and client lock.

Run .venv/bin/python -m pytest tests and git diff --check. Integration must use NEW
disposable containers only with ALLOW_SYNTHETIC_DB_WRITES=1. SQL Server fixtures also
require ACCEPT_SQLSERVER_EULA=Y. Record actual checks and limitations in
specs/verification.md. Review the final diff, fix findings, then review again.
