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
