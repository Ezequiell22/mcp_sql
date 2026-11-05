# Run the MCP in Docker

The image includes PostgreSQL's Python driver, pyodbc, and Microsoft ODBC Driver
18 for SQL Server. It starts as an unprivileged user (UID 10001) and defaults to
MCP stdio. It includes no connection file, test fixtures, database rows, or model
credentials. Database structure is discovered from the actual connection.

## Build and register once

The administrator must accept the Microsoft driver license when building:

```sh
docker build --build-arg ACCEPT_EULA=Y -t atlas-sql-mcp:local .
```

The ODBC installation follows [Microsoft's Debian installation instructions](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server).
Python runtime packages are pinned by `requirements.lock`,
and `requirements-sqlserver.lock`.
`requirements-dev.lock` and test tooling are excluded from the image. The Python base image and Debian
packages receive upstream updates: record the resulting image digest and use
that immutable digest for release deployment after your own verification.

Configure the MCP host to run `docker` with these arguments:

```json
["run", "--rm", "-i", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--cap-drop=ALL", "--security-opt=no-new-privileges", "atlas-sql-mcp:local"]
```

Keep `-i`; omit `-t`, which would interfere with the stdio protocol. The client
then supplies the connection string through `atlas_sql_connect`. Do not put it
in Docker build arguments, the image, command-line arguments, or committed files.
No published port is needed for stdio.

`localhost` in a connection string means the MCP container itself. Use your real
database hostname and a trusted TLS certificate. Docker Desktop exposes the
host as `host.docker.internal`; Linux may require
`--add-host=host.docker.internal:host-gateway`. A database in another container
needs a shared Docker network and its container hostname. Container hostnames
are remote endpoints, so the product's verified TLS rules still apply.

Optional administrator connection files can be mounted read-only and supplied
with `--connections /run/config/connections.json`; local identity inside this
image is `local#atlas`, not the host computer's username. See
[connection configuration](connection-strings.md) for the separate compatibility mode.

## Trust a private database certificate authority

The certificate hostname must match the database hostname in the connection
string. For the packaged Linux image, provision a PEM CA bundle readable by UID
10001, mount it read-only, and forward its path to both drivers:

```sh
docker run --rm -i --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --mount type=bind,src=/absolute/private/company-ca-bundle.pem,dst=/run/certs/ca.pem,readonly \
  --env PGSSLROOTCERT=/run/certs/ca.pem --env SSL_CERT_FILE=/run/certs/ca.pem \
  atlas-sql-mcp:local
```

Register these arguments with the client when using a private CA. A trust bundle
contains public CA certificates, not the database's private key. Include the
public roots needed by any HTTPS identity/JWKS endpoints if using the same bundle
for shared HTTP. These paths must exist **inside the MCP container**; setting a
host-only path in a client does not mount it. Do not disable hostname or certificate
verification to work around an incorrect certificate. For a persistent Compose
installation, use a private override with the same read-only mount and two
environment settings; [the ERP lab override](../labs/erp/compose.mcp.yaml) is a
separate lab-specific example.

## Keep a local container running

For a persistent container visible in Docker Desktop, use the root
[Compose definition](../compose.yaml):

```sh
ACCEPT_EULA=Y docker compose up -d --build
```

This accepts the Microsoft driver license, builds the image and keeps the
`atlas-sql-mcp` container running with restart policy, a read-only root filesystem,
a temporary `/tmp`, dropped capabilities and bounded resources. If a manually started
container already uses that name, stop/remove that old container before switching
to Compose; connection handles disappear with their server process. To stop the
Compose deployment, use `docker compose down`.
The main MCP process waits for stdio input; no HTTP port is exposed in this mode.
Register `docker` as the client command with arguments
`["exec", "-i", "atlas-sql-mcp", "atlas-sql-mcp"]`. Each client launches its own
MCP session inside the running container. Connection/context handles belong to
that process; reconnect after the client process exits. This is a local stdio
installation. Shared HTTP requires the separate authentication setup below.

## Shared HTTP service

Create a private environment file outside source control containing
`ATLAS_SQL_ISSUER`, `ATLAS_SQL_JWKS_URL`, `ATLAS_SQL_PUBLIC_URL`, and
`ATLAS_SQL_CONNECTION_HOSTS`. Use the endpoint allowlist format documented in
[connection strings](connection-strings.md). Run behind your authenticated HTTPS
ingress, preserving the public Host header:

```sh
docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --env-file /absolute/private/atlas-http.env \
  -p 127.0.0.1:8091:8091 atlas-sql-mcp:local \
  --transport streamable-http --host 0.0.0.0 --port 8091
```

For persistent shared deployment, the [HTTP override](../configs/compose.http.yaml)
adds the authenticated transport, loopback backend port and liveness probe:

```sh
ACCEPT_EULA=Y ATLAS_SQL_HTTP_ENV_FILE=/absolute/private/atlas-http.env \
  docker compose -f compose.yaml -f configs/compose.http.yaml up -d --build
```

Set up the HTTPS ingress and identity provider before registering its public URL.
Changing the transport recreates the container and expires existing sessions.

The MCP endpoint requires verified bearer tokens. `GET /healthz` is a separate
public process-liveness response with no database readiness assertion. Configure
ingress timeouts and resources for the deployment. Sessions live only in memory
and expire; reconnect after restarts. Multiple replicas need session affinity.

## Smoke checks

```sh
docker run --rm atlas-sql-mcp:local --help
docker run --rm --entrypoint python atlas-sql-mcp:local \
  -c 'import os, psycopg, pyodbc; assert os.getuid() == 10001; assert "ODBC Driver 18 for SQL Server" in pyodbc.drivers()'
```

These check packaging, not database compatibility or business correctness.
The disposable database integration suite remains outside the runtime image.
