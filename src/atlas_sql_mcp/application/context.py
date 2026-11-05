"""Bounded, principal-owned context/cursor storage and request admission."""

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from atlas_sql_mcp.domain import Principal, SqlError


@dataclass(frozen=True)
class Context:
    principal: Principal
    request_hash: str
    connection_id: str
    fingerprint: str
    policy_version: str
    expires: float
    indexed: bool = False
    dialect: str = "postgresql"
    # Only fingerprints are retained; live typed metadata is never a persistent
    # business catalog. Each group records the exact scope originally disclosed.
    detail_groups: dict[tuple[str, ...], str] = field(default_factory=dict)


@dataclass(frozen=True)
class Cursor:
    principal: Principal
    context_id: str
    focus_hash: str
    offset: int
    expires: float


class ContextStore:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.contexts: dict[str, Context] = {}
        self.cursors: dict[str, Cursor] = {}
        self.rates: dict[Principal, deque] = {}
        self.lock = threading.RLock()

    def admit(self, principal: Principal) -> None:
        now = self.clock()
        with self.lock:
            self.contexts = {k: v for k, v in self.contexts.items() if v.expires > now}
            self.cursors = {k: v for k, v in self.cursors.items() if v.expires > now}
            self.rates = {p: q for p, q in self.rates.items() if q and q[-1] > now - 60}
            queue = self.rates.setdefault(principal, deque())
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if len(queue) >= 60 or len(self.rates) > 1000:
                raise SqlError("RATE_LIMITED", "Identity request budget exceeded.", retryable=True)
            queue.append(now)

    def get(self, key: str, principal: Principal) -> Context:
        with self.lock:
            context = self.contexts.get(key)
        if not context or context.principal != principal or context.expires <= self.clock():
            raise SqlError(
                "CONTEXT_EXPIRED", "Context is unavailable or expired. Request fresh context."
            )
        return context

    def cursor(self, key: str, context_id: str, focus_hash: str, principal: Principal) -> Cursor:
        with self.lock:
            cursor = self.cursors.get(key)
        if (
            not cursor
            or cursor.principal != principal
            or cursor.context_id != context_id
            or cursor.focus_hash != focus_hash
            or cursor.expires <= self.clock()
        ):
            raise SqlError("CONTEXT_EXPIRED", "Cursor is unavailable for this context and focus.")
        return cursor

    def publish(
        self,
        key: str,
        principal: Principal,
        *,
        created: Context | None = None,
        detail: tuple[tuple[str, ...], str] | None = None,
        cursor: tuple[str, Cursor] | None = None,
    ) -> None:
        """Commit handles and disclosure evidence together, after wire validation."""
        with self.lock:
            context = created if created is not None else self.get(key, principal)
            if context.principal != principal or context.expires <= self.clock():
                raise SqlError("CONTEXT_EXPIRED", "Context expired before response completion.")
            if created is not None and (
                len(self.contexts) >= 1000
                or sum(c.principal == principal for c in self.contexts.values()) >= 25
            ):
                raise SqlError("RATE_LIMITED", "Context storage budget reached.", retryable=True)
            if cursor is not None and len(self.cursors) >= 5000:
                raise SqlError("RATE_LIMITED", "Cursor storage budget reached.", retryable=True)
            if detail is not None:
                scope, fingerprint = detail
                previous = context.detail_groups.get(scope)
                if previous is not None and previous != fingerprint:
                    raise SqlError(
                        "CONTEXT_STALE", "Selected metadata changed. Request fresh context."
                    )
                if scope not in context.detail_groups and len(context.detail_groups) >= 100:
                    raise SqlError(
                        "RATE_LIMITED",
                        "Context detail-page budget reached. Request focused context.",
                        retryable=True,
                    )
            # All admission checks precede mutations, including context/cursor quotas.
            if created is not None:
                self.contexts[key] = context
            if detail is not None:
                context.detail_groups[scope] = fingerprint
            if cursor is not None:
                self.cursors[cursor[0]] = cursor[1]
