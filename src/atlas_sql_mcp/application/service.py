"""Connection/context/finalization use cases; no model calls or business-row reads."""

import secrets
import threading
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from atlas_sql_mcp.analysis.bindings import validate_bindings
from atlas_sql_mcp.analysis.postgres_types import normalize_type
from atlas_sql_mcp.application.context import Context, ContextStore, Cursor
from atlas_sql_mcp.application.policy import (
    CONTEXT_SECONDS,
    INPUT_BYTES,
    OUTPUT_BYTES,
    REQUEST_SECONDS,
    capabilities,
    validate_contract_version,
)
from atlas_sql_mcp.budget import request_budget
from atlas_sql_mcp.contracts import SCHEMAS, validate
from atlas_sql_mcp.diagnostics import operation
from atlas_sql_mcp.domain import (
    AnalyzerPort,
    Catalog,
    CatalogPort,
    Principal,
    SqlError,
    canonical,
    digest,
)

POLICY_REVISION = "atlas-sql-policy-2"


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


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
        self.state = ContextStore(clock=lambda: self.clock())
        self._admission = threading.BoundedSemaphore(10)

    def call(self, tool: str, arguments: dict, principal: Principal) -> dict:
        with operation(tool) as event, request_budget(REQUEST_SECONDS):
            result = self._call(tool, arguments, principal)
            code = result.get("error", {}).get("code")
            event["outcome"] = (
                "timeout"
                if code == "VALIDATION_TIMEOUT"
                else "internal_error"
                if code == "INTERNAL_VALIDATION_ERROR"
                else "rejected"
                if code
                else "success"
            )
            if code == "INTERNAL_VALIDATION_ERROR":
                result["error"]["message"] += f" Diagnostic ID: {event['requestId']}."
            return result

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
            self.state.admit(principal)
            if not self._admission.acquire(blocking=False):
                raise SqlError("RATE_LIMITED", "Concurrent request limit reached.", retryable=True)
            try:
                if tool == "atlas_sql_connect":
                    connector = getattr(self.catalog, "connect", None)
                    if not callable(connector):
                        raise SqlError("CONNECTION_DISABLED", "Connection strings are not enabled.")
                    result = connector(arguments, principal)
                elif tool == "atlas_sql_context_get":
                    result = self.context(arguments, principal)
                else:
                    result = self.finalize(arguments, principal)
            finally:
                self._admission.release()
            pending_detail = result.pop("_pendingDetail", None)
            pending_context = result.pop("_pendingContext", None)
            pending_cursor = result.pop("_pendingCursor", None)
            if arguments.get("contractVersion") == "1.2":
                result = self._neutral_result(result)
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
            if tool == "atlas_sql_context_get":
                self.state.publish(
                    result["contextId"],
                    principal,
                    created=pending_context,
                    detail=pending_detail,
                    cursor=pending_cursor,
                )
            return result
        except SqlError as error:
            return error.envelope()
        except Exception:
            return SqlError(
                "INTERNAL_VALIDATION_ERROR", "Validation did not complete; no artifact was issued."
            ).envelope()

    @staticmethod
    def _neutral_result(result: dict) -> dict:
        result = deepcopy(result)
        payload = result.get("artifact", result)
        payload["contractVersion"] = "1.2"
        for old, new in (("postgresMajorVersion", "databaseMajorVersion"),):
            if old in payload:
                payload[new] = payload.pop(old)
        capabilities = payload.get("capabilities", {})
        if "postgresMajorVersions" in capabilities:
            capabilities["databaseMajorVersions"] = capabilities.pop("postgresMajorVersions")
        typed = list(payload.get("parameters", [])) + list(payload.get("outputs", []))
        for relation in payload.get("relations", []):
            typed.extend(relation["columns"])
        for item in typed:
            if "postgresType" in item:
                item["sqlType"] = item.pop("postgresType")
        if "artifact" in result:
            payload["artifactHash"] = digest(
                {k: v for k, v in payload.items() if k not in {"validation", "artifactHash"}}
            )
        return result

    def _fresh(self, context: Context, principal: Principal) -> Catalog:
        loader = self.catalog.index if context.indexed else self.catalog.load
        catalog = loader(principal, context.connection_id)
        if (
            catalog.fingerprint != context.fingerprint
            or self._policy(catalog) != context.policy_version
        ):
            raise SqlError(
                "CONTEXT_STALE", "Authorized metadata or policy changed. Request fresh context."
            )
        return catalog

    def _scoped_fresh(self, context: Context, candidates: dict, principal: Principal) -> Catalog:
        """Recheck only inspected groups needed by SQL, then project parser input."""
        names = set(candidates["relations"])
        inherited = set(candidates["inheritedRelations"])
        with self.state.lock:
            groups = dict(context.detail_groups)
        loaded = {}
        needed = set(names)
        pending = set(names)
        if not pending:
            catalog = self.catalog.load(principal, context.connection_id, relations=())
            if self._policy(catalog) != context.policy_version:
                raise SqlError("CONTEXT_STALE", "Connection policy changed. Request fresh context.")
            return catalog
        while pending:
            name = min(pending)
            choices = [scope for scope in groups if name in scope]
            if not choices:
                raise SqlError(
                    "CONTEXT_INCOMPLETE",
                    "Request context with focus.relations for every SQL base table and inherited dependency before finalizing.",
                    "contextId",
                )
            scope = min(choices, key=lambda value: (len(value), value))
            if scope not in loaded:
                current = self.catalog.load(principal, context.connection_id, relations=scope)
                if (
                    current.fingerprint != groups[scope]
                    or self._policy(current) != context.policy_version
                ):
                    raise SqlError(
                        "CONTEXT_STALE",
                        "Selected metadata or permissions changed. Request fresh context.",
                    )
                loaded[scope] = current
            relations = loaded[scope].relation_map
            pending.discard(name)
            relation = relations.get(name)
            if relation is None:
                raise SqlError("OBJECT_NOT_AUTHORIZED", "A selected dependency is unavailable.")
            for child in relation.descendants if name in inherited else ():
                if child.startswith("<"):
                    raise SqlError(
                        "OBJECT_NOT_AUTHORIZED", "Inherited table access is not authorized."
                    )
                if child not in needed:
                    inherited.add(child)
                    needed.add(child)
                    pending.add(child)
        catalogs = list(loaded.values())
        combined = {}
        for catalog in catalogs:
            for relation in catalog.relations:
                if relation.key in needed:
                    previous = combined.get(relation.key)
                    constraints = {c.name: c for c in relation.constraints}
                    if previous:
                        constraints.update({c.name: c for c in previous.constraints})
                    combined[relation.key] = replace(
                        relation,
                        constraints=tuple(
                            sorted(
                                (
                                    c
                                    for c in constraints.values()
                                    if c.kind != "f"
                                    or f"{c.target_schema}.{c.target_relation}" in needed
                                ),
                                key=lambda c: c.name,
                            )
                        ),
                    )
        return replace(catalogs[0], relations=tuple(combined[k] for k in sorted(combined)))

    @staticmethod
    def _policy(catalog: Catalog) -> str:
        return digest([POLICY_REVISION, catalog.policy_version])

    def context(self, args: dict, principal: Principal) -> dict:
        now = self.clock()
        key = args.get("contextId")
        focus = args.get("focus", {})
        focused = focus.get("relations", [])
        includes = set(focus.get("include", ["columns", "relationships", "businessDefinitions"]))
        page_size = args.get("pageSize", 25)
        focused_details = None
        focus_hash = digest([focus, page_size])
        offset = 0
        if args.get("cursor"):
            if not key:
                raise SqlError("INVALID_CONTRACT", "A cursor requires its original contextId.")
            cursor = self.state.cursor(args["cursor"], key, focus_hash, principal)
            offset = cursor.offset
        scoped_page = bool(
            key
            and focused
            and not focus.get("search")
            and includes.intersection({"columns", "relationships"})
        )
        pending_context = None
        if key:
            context = self.state.get(key, principal)
            if context.connection_id != args["connectionId"] or context.request_hash != digest(
                args["request"]
            ):
                raise SqlError(
                    "CONTEXT_STALE", "Context is bound to another request or connection."
                )
            if context.indexed and focused and scoped_page:
                catalog = self.catalog.load(
                    principal,
                    context.connection_id,
                    relations=tuple(sorted(focused)[offset : offset + page_size]),
                )
                if self._policy(catalog) != context.policy_version:
                    raise SqlError(
                        "CONTEXT_STALE", "Connection policy changed. Request fresh context."
                    )
                focused_details = catalog
            else:
                catalog = self._fresh(context, principal)
        else:
            if args.get("cursor"):
                raise SqlError("INVALID_CONTRACT", "A cursor requires its original contextId.")
            indexed = callable(getattr(self.catalog, "index", None))
            loader = self.catalog.index if indexed else self.catalog.load
            catalog = loader(principal, args["connectionId"])
            validate_contract_version(args, catalog)
            context = Context(
                principal,
                digest(args["request"]),
                catalog.connection_id,
                catalog.fingerprint,
                self._policy(catalog),
                now + CONTEXT_SECONDS,
                indexed=indexed,
                dialect=catalog.dialect,
            )
            key = secrets.token_urlsafe(32)
            pending_context = context
        validate_contract_version(args, catalog)
        relations = list(catalog.relations)
        if focus.get("relations"):
            requested = set(focus["relations"])
            expected_page = (
                set(sorted(focused)[offset : offset + page_size]) if focused_details else requested
            )
            if not expected_page <= catalog.relation_map.keys():
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
        page = relations if focused_details else relations[offset : offset + page_size]
        index_only = context.indexed and (
            not includes.intersection({"columns", "relationships"})
            or (len(catalog.relations) > 100 and not focus.get("relations"))
        )
        pending_detail = None
        if context.indexed and not index_only and page:
            scope = tuple(sorted(r.key for r in page))
            details = focused_details or self.catalog.load(
                principal, context.connection_id, relations=scope
            )
            if self._policy(details) != context.policy_version:
                raise SqlError("CONTEXT_STALE", "Connection policy changed. Request fresh context.")
            fingerprint = details.fingerprint
            with self.state.lock:
                previous = context.detail_groups.get(scope)
                if previous is not None and previous != fingerprint:
                    raise SqlError(
                        "CONTEXT_STALE", "Selected metadata changed. Request fresh context."
                    )
                if scope not in context.detail_groups and len(context.detail_groups) >= 100:
                    raise SqlError(
                        "RATE_LIMITED",
                        "Context detail-page budget reached. Request a new focused context.",
                        retryable=True,
                    )
                pending_detail = (scope, fingerprint)
            details_by_key = details.relation_map
            page = [details_by_key[r.key] for r in page]
        next_cursor = None
        pending_cursor = None
        total_relations = len(focused) if focused_details else len(relations)
        if offset + len(page) < total_relations:
            next_cursor = secrets.token_urlsafe(32)
            pending_cursor = (
                next_cursor,
                Cursor(principal, key, focus_hash, offset + len(page), context.expires),
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
                            type_key: c.sql_type,
                            "nullable": c.nullable,
                            "description": c.description,
                            "descriptionProvenance": "untrusted_database_comment",
                            "typeSchema": c.type_schema,
                        }
                        for c in relation.columns
                    ]
                    if "columns" in includes and not index_only
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
                    if "relationships" in includes and not index_only
                    else [],
                    "inheritedReadsRequireAuthorization": bool(relation.descendants),
                }
            )
        return {
            "_pendingDetail": pending_detail,
            "_pendingContext": pending_context,
            "_pendingCursor": pending_cursor,
            "status": "context",
            "contractVersion": "1.1" if catalog.dialect == "sqlserver" else "1.0",
            "contextId": key,
            "expiresAt": iso(context.expires),
            "connectionId": catalog.connection_id,
            "dialect": catalog.dialect,
            **(
                {"databaseMajorVersion": catalog.database_major}
                if catalog.dialect == "sqlserver"
                else {"postgresMajorVersion": catalog.database_major}
            ),
            "timezone": self.timezone,
            "clock": iso(now),
            "metadataFingerprint": context.fingerprint,
            "policyVersion": context.policy_version,
            "capabilities": self.capabilities(catalog.dialect),
            "relations": payload,
            "businessDefinitions": [{"status": "not_configured"}],
            "coverage": "partial"
            if index_only
            or next_cursor
            or includes != {"columns", "relationships", "businessDefinitions"}
            else "complete",
            "omittedDetails": [
                "No business rows or value lookups are read.",
                "Business definitions must be supplied and confirmed by the host.",
            ]
            + (
                [
                    "This is a live relation-name index. Request focus.relations with exact names to inspect typed columns and relationships before finalization."
                ]
                if index_only
                else []
            )
            + (
                ["Some metadata fields were excluded by focus.include."]
                if len(includes) < 3
                else []
            ),
            "nextCursor": next_cursor,
        }

    capabilities = staticmethod(capabilities)

    def finalize(self, args: dict, principal: Principal) -> dict:
        context = self.state.get(args["contextId"], principal)
        deadline = time.monotonic() + REQUEST_SECONDS
        candidates = (
            self.analyzer.dependencies(args["sql"], context.dialect) if context.indexed else None
        )
        catalog = (
            self._scoped_fresh(context, candidates, principal)
            if context.indexed
            else self._fresh(context, principal)
        )
        type_key = "sqlType" if catalog.dialect == "sqlserver" else "postgresType"
        validate_contract_version(args, catalog)
        if args["contractVersion"] == "1.2" and catalog.dialect == "postgresql":
            args = deepcopy(args)
            for item in args["parameters"] + args["outputs"]:
                if "sqlType" in item:
                    item["postgresType"] = item.pop("sqlType")
        analysis = self.analyzer.analyze(args["sql"], catalog)
        normalize = normalize_type
        binding_options = {}
        if catalog.dialect == "sqlserver":
            from atlas_sql_mcp.analysis.sqlserver import check_default
            from atlas_sql_mcp.analysis.sqlserver import logical_type as tsql_logical
            from atlas_sql_mcp.analysis.sqlserver import normalize_type as tsql_normalize

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
        if context.indexed:
            refreshed = self._scoped_fresh(context, candidates, principal)
            if refreshed.fingerprint != catalog.fingerprint:
                raise SqlError("CONTEXT_STALE", "Selected metadata changed during validation.")
        else:
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
                else {"postgresMajorVersion": catalog.database_major}
            ),
            "sql": args["sql"],
            "parameters": sorted(deepcopy(args["parameters"]), key=lambda p: p["position"]),
            "filters": deepcopy(args["filters"]),
            "outputs": outputs,
            "semantics": deepcopy(semantics),
            "dependencies": {
                **analysis["dependencies"],
                "metadataFingerprint": catalog.fingerprint
                if context.indexed
                else context.fingerprint,
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
