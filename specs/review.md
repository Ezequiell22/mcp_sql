# MCP request-flow review — November 6, 2026

This historical record predates the production hardening and question-only
generator follow-up. See production.md, tech-lead-review.md and verification.md
for the current contract, worker lifecycle, schema defaults and evidence.

## Findings addressed

1. **Question-only requests stopped at an empty default schema.** Discovery now
   enumerates non-system schemas from fresh metadata on every request, ignores
   empty scopes, and chooses one relevant scope from the business question. One
   available scope requires no model selection. Multiple scopes use a bounded
   structured DeepSeek choice, validated against the discovered catalog. No
   selected scope or catalog is reused across requests.
2. **Errors and instructions still delegated database discovery to end users.**
   Public instructions and manual examples now use a business question alone.
   Missing metadata/access reports an administrator configuration issue; ambiguous
   sources ask which business records are intended. The public tool now accepts only question, with no schema or table-name inputs.
3. **Boundary coverage missed the actual failing path.** New SQLite regressions
   exercise an empty default schema through both the generator and MCP tool,
   sequential scope switching, invalid/ambiguous selections, empty databases,
   catalog budgets, and rejection of cross-schema model output. Live PostgreSQL
   and SQL Server requests now include question-only operational/archive cases.
4. **Docker assertions confused different business requests.** Legacy tests for
   filters, wide columns and independent fact aggregation are now restricted to
   their original cases. The new question-only cases check their own grouping or
   anti-join behavior, rather than an unrelated WHERE requirement.
5. **The SQL Server hostname-negative test expected unsupported behavior.** The
   previous fixture supplied an incorrect alternate certificate hostname while
   connecting to `localhost`, a valid certificate name. Microsoft documents that
   on Linux/macOS either the server name or the alternate name may match:
   [ODBC TLS matching rules](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/connection-string-keywords-and-data-source-names-dsns?view=sql-server-ver17).
   The corrected case connects through a local test alias absent from the
   certificate, with no alternate name. This tests a genuine mismatch. Fixtures
   additionally trust a temporary CA and present a signed server leaf. Production
   certificate checks remain enabled; the prior test failure did not establish a
   production TLS vulnerability.

6. **Inspector still allowed the user to supply an invalid schema.** The actual
   failed browser request contained `schema: dbo` against the PostgreSQL demo.
   The expert field was an unnecessary failure path for the intended audience.
   It has been removed from the public contract, prompts and Inspector form.
   The server always performs source selection internally for MCP requests.
7. **Two differently configured MCP containers caused deployment confusion.**
   The legacy and duplicate MCP containers were retired at the user's request,
   after preserving their configuration privately. Compose now owns a single
   named container, `atlas-sql-mcp`, with one Python MCP process. The client
   forwards stdio to that process rather than starting another Python server.
   A file lock rejects simultaneous clients, and a healthcheck confirms readiness.
8. **Docker and application logging were both disabled.** Docker now retains
   rotating JSON-file diagnostics. Correlated structured events trace requests,
   metadata discovery, model stages, SQL validation, timings, counts and failures.
   HTTP status and SQLSTATE codes are logged when available; questions, prompts,
   SQL, credentials and raw dependency errors are not. Private stdio pipes prevent
   Docker logging from capturing initialization instructions or protocol replies.
   An isolated Docker regression checks reconnects, one process, client locking,
   failure stages, request IDs and exclusion of protected content from logs.

## Boundaries rechecked

- Three production Python files; LangChain and DeepSeek; prompts outside Python
  and included in the Docker image; sole public tool `generate_sql`; stdio only.
- Restricted metadata access, sample_rows_in_table_info=0, no generated SELECT,
  EXPLAIN/PREPARE, application-row reads or database-object installation.
- Schema-qualified physical references checked against selected metadata;
  invalid statements, identifiers and cross-scope references rejected.
- Credentials kept in ignored local configuration; raw driver/provider errors
  hidden at the MCP boundary. Private preexisting files and database containers preserved; duplicate MCP
  containers retired as explicitly requested.
- Discovery bounded by deadline, 100 nonempty schemas, 10000 tables and a 64000-byte
  multi-schema selection context. Existing table/column/context limits retained.

## Remaining limitations

The service generates SQL; it does not prove business meaning, compile or execute
queries, estimate cost, or enforce downstream authorization. One base-table
schema per query remains the supported scope; views and cross-schema joins are
not added by this fix. Names and relationships may not explain opaque ERP metrics
or distinguish tenants: business clarification or administrator metadata work can
still be necessary. Catalog size, permissions and provider availability can limit
successful generation. Local verification is not production certification.

The current runtime uses the existing Docker iDempiere ERP database. The project
has a single root `.env`; obsolete alternate env files and the runtime demo
selectors were removed. Existing private connection files remain intact.

## Follow-up findings and second review

9. **The running setup targeted a small synthetic database.** Runtime now reuses
   the existing ERP restricted-reader connection and verified TLS CA on the ERP
   Docker network. Metadata inspection found 912 base tables. No objects or
   business rows were created, changed or read in that database.
10. **An empty model scope selection was always labeled an archive ambiguity.**
    Source selection now distinguishes selected, ambiguous and no-match outcomes.
    Inconsistent or invented model identifiers still fail validation. Business
    clarifications return a structured needs_clarification result, not Tool Error.
    No-match table selection follows the same clarification contract.
11. **Stateless behavior needed direct evidence.** A regression captures actual
    ChatDeepSeek request payloads through a mock HTTP transport across three
    consecutive requests. Each request rescans metadata, sends only its own
    system/human messages, and the first and repeated final requests have identical
    payloads even after an intervening different question. This checks request
    isolation without storing a conversation in MCP.
12. **Multiple environment files obscured the active database.** Settings, Compose
    and provider-test loading now use only root .env. The database network and CA
    mount are explicit Compose settings. No schema/table settings are introduced.

Second review checked response catch ordering (clarifications before generation
errors), safe application-authored messages and log reason codes, fresh scope
construction, retained SQL guard, sample_rows=0 and secret-free image context.
See verification.md for executed checks and limits.
