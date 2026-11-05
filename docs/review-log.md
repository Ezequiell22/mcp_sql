# Tech-lead review and verification

## Pass 1 — initial six recommendations

- Preserved the documented anonymous HTTP identity. Removed the fixed Compose
  container name, parameterized the loopback port, and documented separate projects
  and externally authorized ingress routes for independent trust groups. This is
  process isolation, not authentication or user isolation inside one MCP process.
- Added allowlisted JSON operational events, request correlation, duration and
  success/rejection/timeout/internal-error categories without logging caller data.
- Focused context continuations that fit one page skip full-index rediscovery and
  reuse their scoped detail read; live privilege and fingerprint checks remain.
- Extracted context storage/admission and dialect capabilities from orchestration;
  dependency extraction now uses the injected analyzer port.
- Added a tag release-readiness gate requiring both the Python/PostgreSQL and
  licensed SQL Server 2022/2025 matrices to succeed, including zero-skipped checks
  in the latter. External release publication must require this gate.
- Renamed internal model fields to sql_type/database_major and updated worker
  serialization, adapters and fixtures; public legacy/neutral contracts remain.

Verification on 2026-11-05: 470 isolated tests passed, 125 skipped; static checks,
exported schemas, documentation links, formatting and git diff whitespace checks
passed. PostgreSQL-enabled verification: 577 passed, 18 skipped. SQL Server and
Docker-image workflows were not run: explicit Microsoft license acceptance is
required. These results are local checks, not production certification.


## Pass 2 — scalability and deadline review

Findings corrected:

- Focused lists larger than one page still rediscovered the whole index. Exact-name
  continuation pages now authorize/load only their current page. Cursor ownership,
  expiry and focus binding are validated before a database is opened. Regressions
  verify page scopes and rejection after a selected grant is revoked.
- Finalization launched two identical dependency workers. It now extracts the
  immutable SQL dependencies once, while independently refreshing catalog data
  before and after full analysis.
- Some five-second catalog budgets restarted after connecting. Nested deadlines
  now preserve the shorter outer deadline, and all three catalog entry points
  include connection time. Expired driver errors map to VALIDATION_TIMEOUT rather
  than an authentication/availability error. Fake-clock regressions verify this.
- PostgreSQL comments were limited only after transfer. Fixed catalog SQL now
  applies the existing 4096-character limit before materializing descriptions.

Verification on 2026-11-05: 473 isolated tests passed, 125 skipped; PostgreSQL-enabled
suite: 580 passed, 18 skipped. Static checks, schemas, documentation and whitespace
checks passed. The remaining SQL Server/image workflows still require explicit
license acceptance and were not executed locally.
