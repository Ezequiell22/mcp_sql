# Documentation

| Need | Guide |
| --- | --- |
| Start using the MCP | [User quick start](../README.md#use-it) |
| Install, configure database access or shared HTTP | [Configuration](configuration.md) |
| Run packaged drivers and MCP in a container | [Docker](docker.md) |
| Register a host | [Canonical client templates](../configs/README.md) |
| Understand where the LLM runs and how decisions are validated | [Architecture and orchestration](architecture.md) |
| Supply a connection string | [Formats, credentials and session lifetime](connection-strings.md) |
| Integrate tools and artifacts | [Contract](contract.md), [neutral 1.2 fields](neutral-contract.md) |
| Check SQL Server capabilities and evidence | [SQL Server](sqlserver-support.md) |
| Operate and assess production readiness | [Operations and limits](operations.md) |
| Review fixes, release evidence and supported boundaries | [Release review](release-review.md) |
| Reproduce integration scenarios and review evidence | [Testing](testing.md) |
| Connect to downloaded, populated ERP databases in Docker | [ERP database lab](../labs/erp/README.md) |

Historical references are kept under `archive/`: [original target specification](archive/sql-generation-mcp-spec.md)
and [parser decision](archive/sql-mcp-parser-decision.md). They include superseded
assumptions and unfinished targets; current guides and [executable schemas](../schemas/)
define implemented behavior.
