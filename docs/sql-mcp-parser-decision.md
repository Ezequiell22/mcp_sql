# Standalone SQL analyzer decision

> Update: [optional LangChain generation](langchain-generation.md) supersedes the
> original host-only/two-tool restriction when `ATLAS_SQL_MODEL` is configured.
> Catalog-only database access and deterministic validation remain unchanged.

Current extension: [experimental SQL Server support](sqlserver-support.md) adds a separate dialect adapter and contract 1.1 while preserving PostgreSQL contract 1.0. The original PostgreSQL requirements below remain the baseline; SQL Server native/ODBC certification is still open.
Status: implemented parser choice for the standalone service; advanced release gates remain open. Date: 26 October 2026.

The original reporting application uses Java 21, Spring and JSqlParser 4.9 to enforce a deliberately narrow single-source SQL policy. Relaxing that policy would expand reporting execution permissions and would not supply PostgreSQL-native parsing, advanced catalog/type resolution, or isolated analysis budgets.

The standalone [SQL MCP package](../README.md) uses Python with pinned pglast 8.7, SQLGlot 30.25.0 and the official MCP SDK 2.3.0. [pglast exposes PostgreSQL parser nodes](https://pglast.readthedocs.io/en/latest/api.html); the installed parser reports PostgreSQL 18.6. [SQLGlot's scoped qualification and type annotation](https://github.com/tobymao/sqlglot/blob/main/posts/onboarding.md) provide a second analysis representation. PostgreSQL-specific corrections cover tested differences such as integer SUM/AVG promotion, text returns, date arithmetic and timezone conversions. The resolver never rewrites the returned SQL.

This departs from the target specification's default Java reuse because the native parser is available directly in Python, alongside a maintained MCP SDK, without introducing a native-parser sidecar into the reporting process. Catalog access, policy, connection authorization, context storage, binding checks and protocol remain in one separately installable service. The pure catalog models and ports do not depend on MCP, a driver, or reporting code.

Native syntax acceptance is insufficient for permission or type correctness. The service also walks the complete native tree, checks built-in functions/types/operators, qualifies all base relations, authorizes all discovered columns, checks dependency agreement between parsers, analyzes unique-key paths for aggregate fan-out, and checks supported semantic/type cases. A disposable subprocess bounds crashes and analysis time. Database access uses reviewed catalog statements only; no PREPARE or generated-query execution is used to pretend static resolution is complete.

The spike is covered by executable tests: advanced SQL forms and output types execute against disposable PostgreSQL 18; a multi-CTE monthly cohort analysis exceeding 1,000 lines reconciles with a shorter independent query; safety/ambiguity cases fail explicitly. The real destination is not modified. The test fixture is created in a new Docker container behind the synthetic-write gate.

Trade-offs: an additional deployment runtime, native wheel availability, two representations to reconcile, conservative false rejections, and incomplete coverage of PostgreSQL's overload/coercion and view semantics. Unknown/custom types/functions, unattested views, table-valued functions, and nullable runtime filters remain explicit unsupported cases. A successful artifact records `resolution: passed_supported_subset`, unknown output nullability, unproved business equivalence, and unassessed execution cost. It must not be represented as a full PostgreSQL DBA or server analyzer.

Before broader release, complete the target's client/model evaluations, performance percentiles, view/function dependency attestations, extended type conformance, and report-consumer integration. Any external reporting consumer retains its own execution policy; this repository has no dependency on its service, frontend, metadata schema or release cycle.
