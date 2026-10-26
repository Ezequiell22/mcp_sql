"""Per-request monotonic deadline shared by application and blocking adapters."""

import time
from contextlib import contextmanager
from contextvars import ContextVar

from .domain import SqlError

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
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)
