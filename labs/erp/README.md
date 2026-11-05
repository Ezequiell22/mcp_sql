# ERP database lab

This lab restores existing, populated ERP-product databases published by their
vendors. It does not generate business records or provide a built-in business
catalog to the MCP. The MCP discovers the database connected by the caller; these
databases are separate test destinations, not application dependencies.

## Databases and provenance

| Container | Product and dataset | Host endpoint |
| --- | --- | --- |
| `atlas-erp-postgres` | iDempiere 13, vendor PostgreSQL distribution | `localhost:55432` |
| `atlas-erp-sqlserver-us` | SAP Business One 10.0 FP2608, United States | `localhost:51433` |
| `atlas-erp-sqlserver-br` | SAP Business One 10.0 FP2608, Brazil | `localhost:52433` |

The two SQL Server datasets are distinct localizations of the **same actual ERP
product**, SAP Business One. They are not two different ERP products. SAP provides
them as populated OEC Computers model-company databases, including transactional
data. They are not AdventureWorks, WideWorldImporters, or locally fabricated ERP
schemas.

### iDempiere

- [Official ERP project and source licensing](https://idempiere.org/source-code/).
- [Official version 13 database distribution](https://github.com/idempiere/binary.file/tree/master/database/13).
- [PostgreSQL archive download](https://raw.githubusercontent.com/idempiere/binary.file/master/database/13/Adempiere_pg.jar).
- [Vendor documentation describing extraction and restoration of the PostgreSQL dump](https://wiki.idempiere.org/en/Developing_Plug-Ins_-_Setup_the_workspace).

The archive is a published database dump. Restoring its existing schema and rows
does not run a local business-data generator. The project identifies its source
license as GPLv2 or later; retain the upstream notices accompanying downloaded
material.

### SAP Business One

- [Official localized database overview, including download links](https://help.sap.com/doc/1660bf9ea40a46e1916736665d024dc6/10.0/en-US/B1_Demo_Databases_Overview.pdf).
- [United States archive](https://d.dam.sap.com/a/s7y6Tp4): `SQL_FP2608_United_States.zip`.
- [Brazil archive](https://d.dam.sap.com/a/obLeAHt): `SQL_FP2608_Brazil.zip`.
- [SAP announcement describing the populated environment and its testing purpose](https://community.sap.com/t5/enterprise-resource-planning-blog-posts-by-sap/ready-set-demo-sap-business-one-releases-updated-localized-demo-databases/ba-p/14329028/page/2).
- [Official administrator guide containing the demo-database usage restriction](https://help.sap.com/doc/36687807fb2e47bcbc131196dbbeb753/10.0/en-US/AdministratorGuide_SQL.pdf).

These are proprietary SAP demo assets, not open-source datasets. Use them only
for this isolated demonstration and testing lab. SAP states that demo databases
must not be used in a productive environment. Do not redistribute the archives
or restored backups; obtain them from the official links and retain SAP notices.
The public availability of a download does not grant a license to the SAP ERP
application. This lab runs database engines, not the SAP application.

## Configuration and credentials

[compose.yaml](compose.yaml) defines separate containers and persistent volumes.
Published ports bind only to the local host. Database names, restricted reader
credentials, trusted certificate paths, and complete connection strings belong
in the private lab connection document outside Git:
`/Users/ezequielmenegas/.local/share/atlas-sql-erp-lab/CONNECTIONS.md`.
Do not commit downloaded
archives, database volumes, passwords, environment files, or private TLS keys.

The Compose environment requires `LAB_DIR`, `POSTGRES_PASSWORD`,
`MSSQL_SA_PASSWORD`, and explicit `ACCEPT_SQLSERVER_EULA=Y` from that private setup. Administrative restoration accounts
are for lab setup; MCP sessions must use the restricted reader credentials.
The Microsoft SQL Server Developer image is for development and testing; its
EULA must be accepted before running it. This Compose file preserves an existing
restoration; it is not an automatic download/restore installer. Starting it with
empty volumes creates database engines, not the populated ERP databases. Keep the
private restoration material and backups available before replacing volumes.

When the MCP itself runs in Docker, `localhost` refers to the MCP container.
Use the database hostname and certificate trust configuration recorded in the
private connection document for that deployment, rather than copying a host
endpoint into a container connection string.

Use [mcp-clients.json](mcp-clients.json) for the Docker-backed Claude Code and
Cursor configuration, or [codex-docker.toml](../../configs/codex-docker.toml) for
Codex. The private connection document contains the corresponding database
connection strings. The lab TLS certificate expires approximately 2027-11-04;
renew it before then and update client trust as needed.

## Verification results — 2026-11-05

All three databases were restored and their containers are running. Restricted
reader access and verified TLS were tested. Existing business records were
counted independently of the MCP:

| Database | Base tables | Orders | Invoices | Business partners |
| --- | ---: | ---: | ---: | ---: |
| iDempiere 13 | 912 | 8 | 8 | 18 |
| SAP Business One United States | 2,701 | 1,179 | 1,046 | 24 |
| SAP Business One Brazil | 2,701 | 183 | 159 | 23 |

The iDempiere dump contained an invalid commutator declaration for its
interval-minus-numeric operator. Restoration required recreating that missing
operator using the dump's existing `subtractdays` function, without the invalid
commutator. No business records were generated or changed.

**The previous whole-schema discovery blocker has been resolved.** All three
restricted-reader connection strings now connect successfully. Discovery returns
a live authorized name index, then loads typed details for selected report scopes.
No ERP-specific catalog, generated business records or narrowed reader grants were
introduced to make the tests pass.

The MCP SDK acceptance harness traversed all inventory pages and validated six
reports per database:

| Database | Inventory pages | Relations discovered | Validated reports |
| --- | ---: | ---: | ---: |
| iDempiere 13 | 10 | 912 | 6 |
| SAP Business One United States | 28 | 2,701 | 6 |
| SAP Business One Brazil | 28 | 2,701 | 6 |

Scenarios cover document-status grouping, customer totals, preaggregated line
amounts, ranking without result caps, typed half-open timestamp periods, and
product quantity/amount summaries with joins. The exact SQL is preserved, and
all artifacts state `executionPerformed: false`.

A separate read-only native-driver harness executed all **18** statements and
reconciled them with independent calculations from the original vendor rows.
Each report returned data. The business results were not produced or fetched by
the MCP. Private scenarios, results and credentials remain outside Git in the
local lab directory. The generic [acceptance harness](verify.py) takes private
operator-supplied scenarios; it contains no built-in ERP table mappings.

Full inventory traversal took approximately 7 seconds for iDempiere and
109–111 seconds per SAP database on this local machine. These are complete
catalog walks, not single-report latency or production capacity measurements.
Agents should search the name index and focus relevant tables rather than walk
every page for each report. See [metadata budgets and limitations](../../docs/operations.md).

Creating reader roles and configuring TLS is lab access setup. It does not add
or change the vendor's business records. Keep all database restoration and
inspection outside the production MCP, whose destination access remains limited
to authorized metadata reads.

## Repeatable MCP acceptance checks

Use [verify.py](verify.py) to test the real MCP protocol against operator-supplied
connections and SQL proposals. The harness has no ERP-specific identifiers,
credentials, schema mappings, business-row queries, or model integration. It lists
the three tools, connects, traverses lightweight relation-name pages, requests
focused typed contexts, and
finalizes supplied proposals. It verifies complete SQL preservation and that no
report execution was performed. This is deterministic acceptance, not a live LLM
assessment or a reconciliation of report totals.

Create a private JSON file **outside this repository**, protected so only your OS
account can read it. It contains a `command` array (the MCP executable and arguments)
and a `targets` array. An optional private `env` object forwards operator settings
such as trusted CA paths to a local Python server. Each target contains `connectionString` and `scenarios`.
Each scenario contains `request`, context `focus` with exact `relations` needed
by the SQL, and a `proposal` in
the [neutral finalization input format](../../examples/finalize-neutral.json).
Omit placeholder context IDs: the harness supplies a fresh context and contract
version. Supply SQL and declarations reviewed for the actual database; this file
is an acceptance corpus, never a runtime catalog. Do not copy credentials into
public examples or this guide. The harness never prints connection strings,
SQL, database names, child diagnostics, or raw exception messages.

```sh
.venv/bin/python labs/erp/verify.py /absolute/private/scenarios.local.json
```

The process exits nonzero on connection, discovery, validation or protocol failure.
Targets without a finalization scenario also fail: a successful connection alone
is insufficient acceptance evidence. Output contains target/scenario indices,
counts, status codes and elapsed time. Server admission limits still apply; split
large corpora into separately timed runs. The harness does not start containers,
restore databases, modify permissions, or execute the proposed SQL.
