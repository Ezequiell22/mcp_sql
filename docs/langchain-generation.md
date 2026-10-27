# LangChain generation

The original host-only decision is superseded by optional server-side generation.
Without `ATLAS_SQL_MODEL`, the two existing tools and dependencies remain compatible.
With it, `atlas_sql_generate` accepts the original request and returns the same
validated artifact as `atlas_sql_finalize`, or a structured error.

## Setup

Install the pinned optional dependencies:

```sh
.venv/bin/pip install -r requirements-generation.lock
.venv/bin/pip install --no-deps -e .
```

Provide `ATLAS_SQL_MODEL=openai:YOUR_MODEL_NAME` and `OPENAI_API_KEY` through the
server's protected process environment. Choose a model supporting tool calling.
The included adapter is LangChain's `ChatOpenAI`; compatible endpoints can use
`OPENAI_BASE_URL`. Credentials and endpoints never come from tool arguments.
The server does not load `.env` files automatically. Restart after changing settings.

For Codex, add `ATLAS_SQL_MODEL`, `OPENAI_API_KEY` and, if applicable,
`OPENAI_BASE_URL` to the registration's `env_vars`. Set `tool_timeout_sec = 120`.
For other clients, allow at least 120 seconds per tool call and supply the same
variables using the client's protected environment mechanism. Do not commit keys.
The operator must approve disclosure of request text and authorized metadata to the
configured model provider. Generation makes billable provider calls independently
of any model used by the MCP client. Shared HTTP still requires verified bearer identity.

Example tool arguments:

```json
{
  "contractVersion": "1.0",
  "connectionId": "sales-readonly",
  "request": "List order IDs",
  "relations": ["public.orders"]
}
```

Use version `1.1` for SQL Server. `relations` is optional and limits catalog context
to exact authorized names. The configured connection selects the dialect.

## Small pipeline

1. Fetch authorized catalog context with the existing identity-bound service.
2. Use a structured LangChain prompt to organize the request and identify missing
   business decisions. Return `CLARIFICATION_REQUIRED` with questions if needed.
3. Keep the original request authoritative; provide the refinement as supplementary
   data to the SQL prompt, together with dialect capabilities and the existing schema.
4. Validate each complete proposal with the existing deterministic finalizer. Retry
   only selected repairable errors, with at most three drafts; stop repeated failures.

Only `langchain-core` and `langchain-openai` are added. There is no LangGraph runtime,
generic SQL-execution agent, second catalog layer, or duplicate validation schema.
The prompts are the `SYSTEM`, `REFINE` and `DRAFT` constants in
[`generation.py`](../src/atlas_sql_mcp/generation.py).

The orchestration has a 90-second deadline, 20-second provider timeout, 8,192 output
tokens per model call, no provider
retries, two concurrent generations per process, four catalog pages of 100 relations,
and a 256 KiB model payload budget. The SDK call has a separate 22-second cancellation
bound. Oversized context fails explicitly; select relevant relations rather than
silently truncating metadata. Existing per-identity admission, freshness, authorization,
input/output budgets and isolated validation still apply. Cancellation releases capacity.

No generated SQL is executed. Model proposals cannot select context IDs, identities,
connection credentials or contract versions. Comments, model refinements and error
messages remain data in the human payload rather than becoming system instructions.
These measures reduce risk; prompts do not prove intent preservation or injection immunity.
Artifacts retain `businessEquivalence: not_proven` and `costAssessment: not_performed`.

## Evidence and limits

Automated checks use the real LangChain prompt/runnable pipeline, deterministic
model responses, a real provider adapter over mocked HTTP, both dialect validators,
and authenticated MCP HTTP calls. They cover repair limits, clarification, authorization,
write rejection, malformed model output, metadata pagination, UTF-8 limits, cancellation,
capacity and redacted failures. No provider key or customer data is used in these tests.
Live model quality, provider endpoint compatibility, SQL Server engine certification
and performance under production load still require acceptance testing.

Verification on 26 October 2026: a clean installation of the generation lock passed
424 tests, including the disposable PostgreSQL fixture; six optional native SQL Server
checks were skipped. Syntax, schema consistency, local documentation links, lint,
formatting, dependency consistency and wheel build also passed. Python 3.12 wheel
dependency resolution was checked; the local test runtime was Python 3.14.
