"""Per-request monotonic deadline shared by application and blocking adapters."""

import time
from contextlib import contextmanager
from contextvars import ContextVar

from atlas_sql_mcp.domain import SqlError

METADATA_SECONDS = 5

_deadline: ContextVar[float | None] = ContextVar("atlas_sql_deadline", default=None)


def remaining(maximum: float) -> float:
    deadline = _deadline.get()
    value = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if value <= 0:
        raise SqlError(
            "VALIDATION_TIMEOUT", "Request exceeded its total time budget.", retryable=True
        )
    return value


@contextmanager
def request_budget(seconds: float):
    outer = _deadline.get()
    deadline = time.monotonic() + seconds
    token = _deadline.set(deadline if outer is None else min(outer, deadline))
    try:
        yield
    finally:
        _deadline.reset(token)
