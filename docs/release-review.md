# Release review — 5 November 2026

## Architecture and user workflow

The connected agent supplies the only LLM. The MCP exposes three tools: connect,
live metadata context, and deterministic SQL finalization. The user supplies a
connection string and a report request; the agent handles identifiers, pagination,
SQL dialect, parameters and repairs. No server model, provider key, built-in ERP
catalog, business-row lookup or generated-query execution exists in the runtime.

The supported engines are PostgreSQL 18 and SQL Server 2022/2025, within their
published static SQL subsets. Different dialects have dedicated metadata, type
and permission adapters. Unknown features fail explicitly.

## Review and corrections

Three subagents independently reviewed architecture/metadata, validation/security,
and usability/configuration/documentation. Findings were fixed and reviewed again.

| Finding | Correction and evidence |
| --- | --- |
| Whole-database size ceilings blocked genuine ERPs | Streaming authorized name indexes and scoped typed metadata; all three vendor databases connect and paginate |
| Entire catalog exceeded parser input | Isolated dependency extraction, mandatory inspected scopes, and projection to SQL dependencies |
| Metadata freshness did not cover schema USAGE revocation | Live PostgreSQL schema permission check and a native revocation regression |
| Failed oversized pages counted as inspected | Scope registration commits only after response validation and wire-size checks |
| Names-only fingerprints were used as typed evidence | Artifacts attest the actual typed dependency catalog |
| Inherited reads and ONLY used identical scope rules | Separate inheritance-aware dependency extraction and regression tests |
| PostgreSQL uppercase names and SAP mixed-case identifiers failed | Dialect-specific identifier handling; real ERP reports and quoted-name regressions |
| Invalid grouping, JOIN aggregates and DISTINCT ordering could pass | Conservative scope checks and native-backed positive/negative cases |
| SQL Server integer arithmetic and legacy types were incomplete | Native type conformance, timestamp defaults and MAX text support |
| Optional server LLM conflicted with the architecture | Provider code, dependencies, tool, schemas, environment example and image override removed |
| Verification and documentation missed lab assets | One verification entry point, generic private-input acceptance harness, fresh-install and CA instructions |

Metadata has bounded transfer and retained memory budgets. Expiry releases
capacity; admission is atomic. These controls do not cap report rows or insert
LIMIT/TOP. Credentials remain in expiring process memory and outside diagnostics.

## Verification

- Strict suite: **610 passed, zero skipped**, 74.21 seconds.
- Additional SQL Server matrix: the same **18 native/ODBC/HTTPS checks passed on
  2022 and on 2025**, with actual server-version assertions and no skips.
- Genuine ERP acceptance: **66 inventory pages, 6,314 tables, 18 validated reports**.
- Independent native execution: all **18 reports reconciled** against calculations
  from original vendor rows; each report returned data. This harness is outside MCP.
- Ten concurrent fixture requests/finalizations passed; observed maxima were
  0.0699 and 0.6906 seconds. These are local fixture observations, not load percentiles.
- Lint, formatting, schemas, configuration and documentation consistency passed.
- Wheel inspection found no model provider, test fixture, lab data or credentials.

The local runtime image ID is `sha256:9b13334e7e61642ce7b1af0a36f1f4729b6b43df1d11de06768679f2b4902ff9`.
The MCP and three persistent ERP containers remain running. Temporary integration
containers and networks were removed. The SQL Server 2025 test image was
`mcr.microsoft.com/mssql/server@sha256:245f6b1c8903edae52cb20fa31419cefcde1a49938bb8c3447099011fc871ca3`.
CI has a license-gated 2022/2025 matrix; these results are local, not remote CI results.

## Release boundaries

No additional concrete defect remained from the final targeted reviews. This is
not a guarantee of zero defects or a certification of arbitrary SQL/ERP schemas.
Unsupported types and objects remain explicit; see the published capabilities.
Static validation does not prove business definitions, runtime success or plan
performance. Customer identity, certificates, expected load and the selected
agent's language-to-SQL quality must be verified in that deployment. Sessions
are process-local; shared replicas require affinity and gateway admission.

See [testing](testing.md), [operations and limits](operations.md),
[architecture](architecture.md) and [the ERP lab](../labs/erp/README.md).
