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
