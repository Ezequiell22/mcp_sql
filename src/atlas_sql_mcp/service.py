"""Two use cases. No transport, report persistence, model calls, or business-row reads."""

import secrets
import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .analyzer import FUNCTIONS, MAX_DEPTH, MAX_NODES, MAX_SQL_BYTES, normalize_type
from .bindings import validate_bindings
from .budget import request_budget
from .contracts import SCHEMAS, validate
from .domain import AnalyzerPort, Catalog, CatalogPort, Principal, SqlError, canonical, digest

POLICY_REVISION = "atlas-sql-policy-2"
INPUT_BYTES = 512 * 1024
OUTPUT_BYTES = 1024 * 1024
CONTEXT_SECONDS = 600


def validate_contract_version(args: dict, catalog: Catalog) -> None:
    expected = "1.1" if catalog.dialect == "sqlserver" else "1.0"
    if args["contractVersion"] != expected:
        raise SqlError(
            "INVALID_CONTRACT",
            f"This connection requires contractVersion {expected}.",
            "contractVersion",
        )


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


@dataclass(frozen=True)
class Context:
    principal: Principal
    request_hash: str
    connection_id: str
    fingerprint: str
    policy_version: str
    expires: float


@dataclass(frozen=True)
class Cursor:
    principal: Principal
    context_id: str
    focus_hash: str
    offset: int
    expires: float


class SqlService:
    def __init__(
        self, catalog: CatalogPort, analyzer: AnalyzerPort, *, clock=time.time, timezone_name="UTC"
    ):
        ZoneInfo(timezone_name)
        self.catalog, self.analyzer, self.clock, self.timezone = (
            catalog,
            analyzer,
            clock,
            timezone_name,
        )
        self._contexts: dict[str, Context] = {}
        self._cursors: dict[str, Cursor] = {}
        self._rates: dict[Principal, deque] = {}
        self._lock = threading.RLock()
        self._admission = threading.BoundedSemaphore(10)

    def call(self, tool: str, arguments: dict, principal: Principal) -> dict:
        with request_budget(10):
            return self._call(tool, arguments, principal)

    def _call(self, tool: str, arguments: dict, principal: Principal) -> dict:
        try:
            if tool not in SCHEMAS:
                raise SqlError("UNKNOWN_TOOL", "Tool is unavailable.")
            if len(canonical(arguments)) > INPUT_BYTES:
                raise SqlError("INPUT_TOO_LARGE", "Tool input exceeds 512 KiB.")
            validate(SCHEMAS[tool][0], arguments)
            if tool == "atlas_sql_context_get" and len(arguments["request"].encode()) > 16384:
                raise SqlError(
                    "INPUT_TOO_LARGE", "Natural-language request exceeds 16 KiB UTF-8.", "request"
                )
            self._admit(principal)
            if not self._admission.acquire(blocking=False):
                raise SqlError("RATE_LIMITED", "Concurrent request limit reached.", retryable=True)
            try:
                result = (
                    self.context(arguments, principal)
                    if tool == "atlas_sql_context_get"
                    else self.finalize(arguments, principal)
                )
            finally:
                self._admission.release()
            validate(SCHEMAS[tool][1], result)
            # MCP returns one canonical text fallback alongside structured content.
            # Reserve extra room for envelope/JSON escaping, not just 2x raw bytes.
            wire = {
                "content": [{"type": "text", "text": canonical(result).decode()}],
                "structuredContent": result,
                "isError": False,
            }
            if len(canonical(wire)) > OUTPUT_BYTES - 2048:
                raise SqlError(
                    "OUTPUT_TOO_LARGE",
                    "Complete response exceeds 1 MiB. Focus metadata or reduce the output contract; SQL is never truncated.",
                )
            return result
        except SqlError as error:
            return error.envelope()
        except Exception:
            return SqlError(
                "INTERNAL_VALIDATION_ERROR", "Validation did not complete; no artifact was issued."
            ).envelope()

    def _admit(self, principal: Principal) -> None:
        now = self.clock()
        with self._lock:
            self._contexts = {k: v for k, v in self._contexts.items() if v.expires > now}
            self._cursors = {k: v for k, v in self._cursors.items() if v.expires > now}
            self._rates = {p: q for p, q in self._rates.items() if q and q[-1] > now - 60}
            queue = self._rates.setdefault(principal, deque())
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if len(queue) >= 60 or len(self._rates) > 1000:
                raise SqlError("RATE_LIMITED", "Identity request budget exceeded.", retryable=True)
            queue.append(now)

    def _get(self, key: str, principal: Principal) -> Context:
        with self._lock:
            context = self._contexts.get(key)
        if not context or context.principal != principal or context.expires <= self.clock():
            raise SqlError(
                "CONTEXT_EXPIRED", "Context is unavailable or expired. Request fresh context."
            )
        return context

    def _fresh(self, context: Context, principal: Principal) -> Catalog:
        catalog = self.catalog.load(principal, context.connection_id)
        if (
            catalog.fingerprint != context.fingerprint
            or self._policy(catalog) != context.policy_version
        ):
            raise SqlError(
                "CONTEXT_STALE", "Authorized metadata or policy changed. Request fresh context."
            )
        return catalog

    @staticmethod
    def _policy(catalog: Catalog) -> str:
        return digest([POLICY_REVISION, catalog.policy_version])

    def context(self, args: dict, principal: Principal) -> dict:
        now = self.clock()
        key = args.get("contextId")
        if key:
            context = self._get(key, principal)
            if context.connection_id != args["connectionId"] or context.request_hash != digest(
                args["request"]
            ):
                raise SqlError(
                    "CONTEXT_STALE", "Context is bound to another request or connection."
                )
            catalog = self._fresh(context, principal)
        else:
            if args.get("cursor"):
                raise SqlError("INVALID_CONTRACT", "A cursor requires its original contextId.")
            catalog = self.catalog.load(principal, args["connectionId"])
            validate_contract_version(args, catalog)
            context = Context(
                principal,
                digest(args["request"]),
                catalog.connection_id,
                catalog.fingerprint,
                self._policy(catalog),
                now + CONTEXT_SECONDS,
            )
            key = secrets.token_urlsafe(32)
            with self._lock:
                if (
                    len(self._contexts) >= 1000
                    or sum(c.principal == principal for c in self._contexts.values()) >= 25
                ):
                    raise SqlError(
                        "RATE_LIMITED", "Context storage budget reached.", retryable=True
                    )
                self._contexts[key] = context
        validate_contract_version(args, catalog)
        focus = args.get("focus", {})
        page_size = args.get("pageSize", 25)
        focus_hash = digest([focus, page_size])
        offset = 0
        if args.get("cursor"):
            with self._lock:
                cursor = self._cursors.get(args["cursor"])
            if (
                not cursor
                or cursor.principal != principal
                or cursor.context_id != key
                or cursor.focus_hash != focus_hash
                or cursor.expires <= now
            ):
                raise SqlError(
                    "CONTEXT_EXPIRED", "Cursor is unavailable for this context and focus."
                )
            offset = cursor.offset
        relations = list(catalog.relations)
        if focus.get("relations"):
            requested = set(focus["relations"])
            if not requested <= catalog.relation_map.keys():
                raise SqlError(
                    "OBJECT_NOT_AUTHORIZED", "Requested metadata is unavailable for this identity."
                )
            relations = [r for r in relations if r.key in requested]
        if focus.get("search"):
            words = focus["search"].lower().split()
            relations = [
                r
                for r in relations
                if any(
                    w in f"{r.key} {r.description} {' '.join(c.name for c in r.columns)}".lower()
                    for w in words
                )
            ]
        includes = set(focus.get("include", ["columns", "relationships", "businessDefinitions"]))
        page = relations[offset : offset + page_size]
        next_cursor = None
        if offset + len(page) < len(relations):
            with self._lock:
                if len(self._cursors) >= 5000:
                    raise SqlError("RATE_LIMITED", "Cursor storage budget reached.", retryable=True)
                next_cursor = secrets.token_urlsafe(32)
                self._cursors[next_cursor] = Cursor(
                    principal, key, focus_hash, offset + len(page), context.expires
                )
        type_key = "sqlType" if catalog.dialect == "sqlserver" else "postgresType"
        payload = []
        for relation in page:
            payload.append(
                {
                    "schema": relation.schema,
                    "name": relation.name,
                    "kind": relation.kind,
                    "description": relation.description,
                    "descriptionProvenance": "untrusted_database_comment",
                    "columns": [
                        {
                            "name": c.name,
                            type_key: c.postgres_type,
                            "nullable": c.nullable,
                            "description": c.description,
                            "descriptionProvenance": "untrusted_database_comment",
                            "typeSchema": c.type_schema,
                        }
                        for c in relation.columns
                    ]
                    if "columns" in includes
                    else [],
                    "relationships": [
                        {
                            "name": c.name,
                            "kind": c.kind,
                            "columns": list(c.columns),
                            "targetSchema": c.target_schema,
                            "targetRelation": c.target_relation,
                            "targetColumns": list(c.target_columns),
                            "validated": c.validated,
                            "enforced": c.enforced,
                        }
                        for c in relation.constraints
                    ]
                    if "relationships" in includes
                    else [],
                    "inheritedReadsRequireAuthorization": bool(relation.descendants),
                }
            )
        return {
            "status": "context",
            "contractVersion": "1.1" if catalog.dialect == "sqlserver" else "1.0",
            "contextId": key,
            "expiresAt": iso(context.expires),
            "connectionId": catalog.connection_id,
            "dialect": catalog.dialect,
            **(
                {"databaseMajorVersion": catalog.database_major}
                if catalog.dialect == "sqlserver"
                else {"postgresMajorVersion": catalog.postgres_major}
            ),
            "timezone": self.timezone,
            "clock": iso(now),
            "metadataFingerprint": context.fingerprint,
            "policyVersion": context.policy_version,
            "capabilities": self.capabilities(catalog.dialect),
            "relations": payload,
            "businessDefinitions": [{"status": "not_configured"}],
            "coverage": "partial"
            if next_cursor or includes != {"columns", "relationships", "businessDefinitions"}
            else "complete",
            "omittedDetails": [
                "No business rows or value lookups are read.",
                "Business definitions must be supplied and confirmed by the host.",
            ]
            + (
                ["Some metadata fields were excluded by focus.include."]
                if len(includes) < 3
                else []
            ),
            "nextCursor": next_cursor,
        }

    @staticmethod
    def capabilities(dialect="postgresql") -> dict:
        if dialect == "sqlserver":
            from .sqlserver import FUNCTIONS as TSQL_FUNCTIONS
            from .sqlserver import SESSION_SETTINGS

            base = SqlService.capabilities()
            base.pop("postgresMajorVersions")
            base["databaseMajorVersions"] = [16, 17]
            base["requiredSessionSettings"] = dict(SESSION_SETTINGS)
            base["features"] = [
                "qualified_base_tables",
                "governed_joins",
                "nonrecursive_ctes",
                "derived_tables",
                "numeric_aggregates",
                "conditional_aggregates",
                "ranking_windows",
                "typed_named_parameters",
                "ordered_top",
            ]
            base["unsupported"] += [
                "native_server_syntax_verification",
                "set_operations",
                "recursive_ctes",
                "aggregate_windows",
                "implicit_type_coercion",
                "decimal_scale_reduction",
                "query_hints",
                "temporal_tables",
                "computed_or_masked_columns",
            ]
            base["approvedFunctions"] = sorted(TSQL_FUNCTIONS | {"cast"})
            return base
        return {
            "postgresMajorVersions": [18],
            "features": [
                "qualified_base_tables",
                "governed_joins",
                "ctes",
                "recursive_ctes",
                "correlated_subqueries",
                "window_functions",
                "calculations",
                "grouping_sets",
                "set_operations",
                "typed_positional_parameters",
                "large_queries",
            ],
            "unsupported": [
                "views_without_dependency_attestation",
                "custom_types_or_functions",
                "projection_wildcards",
                "natural_or_using_joins",
                "cartesian_joins",
                "table_valued_functions",
                "nullable_runtime_parameters",
                "query_execution",
                "cost_estimation",
            ],
            "approvedFunctions": sorted(FUNCTIONS),
            "requiredSessionSettings": {"search_path": "pg_catalog"},
            "limits": {
                "sqlBytes": MAX_SQL_BYTES,
                "inputBytes": INPUT_BYTES,
                "outputBytes": OUTPUT_BYTES,
                "astNodes": MAX_NODES,
                "astDepth": MAX_DEPTH,
                "contextSeconds": CONTEXT_SECONDS,
                "analysisSeconds": 7,
            },
            "businessRowsRead": False,
            "queryExecutionAvailable": False,
            "parameterCastsRequired": True,
        }

    def finalize(self, args: dict, principal: Principal) -> dict:
        context = self._get(args["contextId"], principal)
        deadline = time.monotonic() + 10
        catalog = self._fresh(context, principal)
        type_key = "sqlType" if catalog.dialect == "sqlserver" else "postgresType"
        validate_contract_version(args, catalog)
        analysis = self.analyzer.analyze(args["sql"], catalog)
        normalize = normalize_type
        binding_options = {}
        if catalog.dialect == "sqlserver":
            from .sqlserver import check_default
            from .sqlserver import logical_type as tsql_logical
            from .sqlserver import normalize_type as tsql_normalize

            normalize = tsql_normalize
            binding_options = {
                "type_key": type_key,
                "normalize": normalize,
                "logical": tsql_logical,
                "default_check": check_default,
            }
        validate_bindings(
            args["parameters"], args["filters"], analysis["parameters"], **binding_options
        )
        for item in args["filters"]:
            if item["kind"].endswith("_range"):
                expressions, clauses = set(), set()
                for name, field in item["fields"].items():
                    expected = (
                        ("gte" if field.get("inclusive", True) else "gt")
                        if name == "from"
                        else ("lte" if field.get("inclusive", False) else "lt")
                    )
                    actual_ops = analysis["parameterPredicates"].get(field["parameterPosition"], [])
                    if not actual_ops or any(op != expected for op in actual_ops):
                        raise SqlError(
                            "PARAMETER_MISMATCH",
                            "Range inclusivity disagrees with the actual SQL predicates.",
                            "filters",
                        )
                    for proof in analysis["parameterRanges"].get(field["parameterPosition"], []):
                        if not proof["conjunctive"]:
                            raise SqlError(
                                "PARAMETER_MISMATCH",
                                "Range comparisons must be mandatory conjunctive predicates.",
                                "filters",
                            )
                        clauses.add(proof["clause"])
                    expressions.update(
                        analysis["parameterExpressions"].get(field["parameterPosition"], [])
                    )
                if None in expressions or len(expressions) != 1 or len(clauses) != 1:
                    raise SqlError(
                        "PARAMETER_MISMATCH",
                        "Range endpoints must constrain the same row or aggregate expression.",
                        "filters",
                    )
        declared = {o["key"]: o for o in args["outputs"]}
        actual = {o["key"]: o for o in analysis["outputs"]}
        if len(declared) != len(args["outputs"]) or declared.keys() != actual.keys():
            raise SqlError(
                "OUTPUT_CONTRACT_MISMATCH",
                "Declared output keys must exactly match query outputs.",
                "outputs",
            )
        outputs = []
        for derived in analysis["outputs"]:
            supplied = declared[derived["key"]]
            if type_key in supplied and normalize(supplied[type_key]) != derived[type_key]:
                raise SqlError(
                    "OUTPUT_CONTRACT_MISMATCH",
                    "Declared output type disagrees with analyzed type.",
                    "outputs",
                )
            outputs.append(
                {**derived, **{k: v for k, v in supplied.items() if k in {"label", "unit"}}}
            )
        semantics = args["semantics"]
        if not set(semantics["grain"]) <= actual.keys() or any(
            m["outputKey"] not in actual for m in semantics["measures"]
        ):
            raise SqlError(
                "AMBIGUOUS_GRAIN",
                "Grain and measure keys must reference result outputs.",
                "semantics",
            )
        # Catch changes while analysis was in progress; no stale success is issued.
        self._fresh(context, principal)
        if time.monotonic() > deadline:
            raise SqlError(
                "VALIDATION_TIMEOUT", "Finalization exceeded its total time budget.", retryable=True
            )
        if context.expires <= self.clock():
            raise SqlError(
                "CONTEXT_EXPIRED", "Context expired during analysis; request fresh context."
            )
        artifact = {
            "contractVersion": "1.1" if catalog.dialect == "sqlserver" else "1.0",
            "dialect": catalog.dialect,
            **(
                {"databaseMajorVersion": catalog.database_major}
                if catalog.dialect == "sqlserver"
                else {"postgresMajorVersion": catalog.postgres_major}
            ),
            "sql": args["sql"],
            "parameters": sorted(deepcopy(args["parameters"]), key=lambda p: p["position"]),
            "filters": deepcopy(args["filters"]),
            "outputs": outputs,
            "semantics": deepcopy(semantics),
            "dependencies": {
                **analysis["dependencies"],
                "metadataFingerprint": context.fingerprint,
                "policyVersion": context.policy_version,
            },
            "requiredSessionSettings": self.capabilities("sqlserver")["requiredSessionSettings"]
            if catalog.dialect == "sqlserver"
            else {"search_path": "pg_catalog"},
            "validation": {
                "syntax": "passed_static_subset" if catalog.dialect == "sqlserver" else "passed",
                "resolution": "passed_supported_subset",
                "authorization": "passed",
                "readOnlyPolicy": "passed",
                "bindings": "passed",
                "semantics": "declared_rules_only",
                "executionPerformed": False,
                "costAssessment": "not_performed",
                "businessEquivalence": "not_proven",
                "verifiedAt": iso(self.clock()),
                "contextExpiresAt": iso(context.expires),
                "limitations": [
                    "Business definitions and declared grain are host assertions, not proof of language-to-SQL equivalence.",
                    "Null output nullability means unknown; no nonnull guarantee is issued.",
                    "Supported static types do not prove all database coercion/overload semantics.",
                    "SQL Server native syntax is not verified; only the published static subset is checked."
                    if catalog.dialect == "sqlserver"
                    else "Execution requires fresh consumer authorization, dependency checks, and enforced search_path=pg_catalog.",
                ],
            },
            "warnings": analysis["warnings"],
            "explanation": "SQL and filter bindings passed the published static checks. The generated query was not executed.",
        }
        # Time-dependent evidence is excluded so identical immutable contracts hash
        # identically, while policy/schema/bindings remain part of integrity identity.
        artifact["artifactHash"] = digest({k: v for k, v in artifact.items() if k != "validation"})
        return {"status": "validated", "artifact": artifact}
