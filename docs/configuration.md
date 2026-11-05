# Installation and configuration

## The connected agent provides the LLM

The user needs only a connection string and a question. The administrator installs
the server and registers it once. The LLM runs in the user's MCP host (for example,
Codex or Claude); the MCP never calls an LLM and accepts no model provider configuration.
See [who decides what](architecture.md).

For Docker, follow [the image and registration guide](docker.md). It includes both
database drivers. For a local Python install, use Python 3.12+ on macOS or Linux:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
```

SQL Server also needs `requirements-sqlserver.lock` and Microsoft ODBC Driver 18;
see [SQL Server setup](sqlserver-support.md). A platform without a compatible
native parser wheel may need a C build toolchain.

Register one canonical [client template](../configs/README.md). Replace its executable
path; preserve the client's other MCP entries. Start without `--connections` for
connection-string discovery. Read [accepted strings and credential handling](connection-strings.md).
The server does not load `.env` files automatically. Supply variables through the
process environment, the client's protected mechanism, or Docker `--env-file`.
The [default Compose service](../compose.yaml) is local stdio and forwards only timezone.
HTTP environment settings below configure identity and network policy, not an LLM.

## Command-line options

| Option | Default | Purpose |
| --- | --- | --- |
| `--transport` | `stdio` | Local protocol; `streamable-http` enables authenticated shared service |
| `--host` | `127.0.0.1` | HTTP listen address; container HTTP needs `0.0.0.0` |
| `--port` | `8091` | HTTP listen port |
| `--connections` | absent | Optional private operator-managed grants file |

## Environment reference

| Variable | Required when | Meaning |
| --- | --- | --- |
| `ATLAS_SQL_TIMEZONE` | Optional | Valid IANA timezone; defaults to `UTC`; never rewrites SQL |
| `ATLAS_SQL_ISSUER` | Shared HTTP | Trusted HTTPS OAuth issuer |
| `ATLAS_SQL_JWKS_URL` | Shared HTTP | Trusted HTTPS signing-key endpoint |
| `ATLAS_SQL_PUBLIC_URL` | Shared HTTP | Public HTTPS resource URL ending in `/mcp` |
| `ATLAS_SQL_CONNECTION_HOSTS` | HTTP connection strings | Comma-separated exact `host:port` allowlist; unset disables this networking |
| `SSL_CERT_FILE` | Packaged Linux SQL Server/private HTTPS CA trust if needed | Mounted PEM CA bundle; include all required trust roots |
| `PGSSLROOTCERT` | PostgreSQL certificate trust if needed | Operator-provisioned trusted CA location available to the server |
| `PGPASSFILE` | Optional configured PostgreSQL passfile | Protected file path; caller strings require a password instead |
| Any configured `passwordEnv` name | Operator-managed connections | Reference to a secret provisioned in the process environment |

SQL Server trust uses the installed ODBC/system certificate store; mount or install
the appropriate CA as described in the [Docker guide](docker.md). SQL Server never
exposes a certificate-verification bypass. PostgreSQL remote connections require
`verify-full`; the connection-string parser permits unencrypted literal loopback/localhost
only as documented in [connection strings](connection-strings.md).

Build-only setting `ACCEPT_EULA` belongs to the Docker guide;
`ALLOW_SYNTHETIC_DB_WRITES`, `ACCEPT_SQLSERVER_EULA` and `ATLAS_SQL_DOCKER_IMAGE`
are test controls in the [testing guide](testing.md), not customer configuration.

## Optional administrator-managed connections

Copy [the connection example](../configs/connections.example.json) to a private file outside the repository, or the ignored `connections.local.json`. Replace the database details and `local#REPLACE_WITH_OS_USERNAME` with `local#` followed by the actual OS username. The example names and tables are illustrative; this package does not create them.

- `grants` explicitly maps an authenticated principal to schema-qualified tables and exact allowed columns. Wildcard grants are rejected. Database permissions are intersected with these grants.
- `passwordEnv` names an externally provisioned variable; its value never enters a tool argument or result. An empty value selects libpq's normal credential lookup, including an OS-protected passfile via `PGPASSFILE`. Do not place a password in the configuration JSON. In the alternative connection-string workflow, only `atlas_sql_connect` receives the string; never include credentials in other tool arguments.
- Local stdio always uses the OS account as its identity. There is no tool argument or CLI flag that claims another local user.
- Remote PostgreSQL hosts require `sslmode: "verify-full"` and the appropriate trusted certificate configuration. Disabling TLS is accepted only for literal loopback addresses.
- The role must be neither superuser nor BYPASSRLS. It must be able to read permitted relation/column metadata and possess SELECT privileges for the permitted columns. Do not use the migration owner.
- The connection file is reloaded on every catalog request, including finalization checks. Invalid/revoked policy fails closed; a restart is not needed to revoke a grant. Secret references remain operator configuration.
- No destination `reporting.database_identity`, helper view, Atlas schema, or reporting metadata migration is required.

Start local stdio:

```sh
.venv/bin/atlas-sql-mcp --connections /absolute/path/connections.local.json
```

MCP uses stdout for protocol messages. Register the absolute executable/config paths in the client using [the Codex example](../configs/codex.toml) or [the Claude example](../configs/claude-code.json). Ensure the client process can access the protected credential source. These are configuration templates, not claims that those specific applications have passed manual acceptance testing.

## Shared Streamable HTTP

Shared mode requires bearer authentication; there is no unauthenticated HTTP mode. Provision a trusted HTTPS ingress and an OAuth issuer that issues RS256 JWT access tokens with:

- `iss` matching `ATLAS_SQL_ISSUER`;
- `aud` matching the full `ATLAS_SQL_PUBLIC_URL`, including `/mcp`;
- valid `exp`, a nonempty `sub`, and scope `atlas:sql`.

Set `ATLAS_SQL_JWKS_URL` to the trusted issuer's HTTPS JWKS endpoint. Configure connection grant identities as `issuer#subject`, for example `https://identity.example.test#analyst`. Issuer URLs cannot contain fragments. Then run:

```sh
.venv/bin/atlas-sql-mcp \
  --connections /absolute/path/shared-connections.json \
  --transport streamable-http --host 127.0.0.1 --port 8091
```

`ATLAS_SQL_PUBLIC_URL` must be an HTTPS URL ending in `/mcp`. Its authority is the allowed Host; supplied browser Origins are rejected. The trusted ingress must preserve that Host, terminate TLS, restrict backend reachability, and enforce request/read timeouts. OAuth protected-resource metadata is served at `/.well-known/oauth-protected-resource/mcp`. Configure public client registration/PKCE at the issuer; the service does not provision identity-provider clients. JWKS metadata is cached for up to five minutes; token expiry/signature/audience/issuer and scope checks apply on every request.

`ATLAS_SQL_TIMEZONE` is optional (default `UTC`, valid IANA timezone). SQL must use explicit date/timezone decisions; this setting does not rewrite SQL. Shared transport has automated SDK tests with signed tokens; real hosted-client onboarding, ingress deployment, and issuer integration remain acceptance work for the chosen environment.
