"""Predicate-based termination middleware for AF Agent loops.

`terminate_when(predicate, reason=...)` returns a `FunctionMiddleware` that
checks `predicate()` after every tool dispatch and raises
`MiddlewareTermination(reason)` once it's true. Eval-specific state lives in
the closure passed into `predicate` — the middleware is otherwise stateless.
"""

from collections.abc import Callable
from typing import Any

from agent_framework import FunctionInvocationContext, FunctionMiddleware, MiddlewareTermination


class _PredicateTermination(FunctionMiddleware):
    def __init__(self, predicate: Callable[[], bool], reason: str) -> None:
        self._predicate = predicate
        self._reason = reason

    async def process(self, context: FunctionInvocationContext, call_next: Any) -> None:
        await call_next()
        if self._predicate():
            raise MiddlewareTermination(self._reason)


def terminate_when(predicate: Callable[[], bool], *, reason: str) -> FunctionMiddleware:
    """`FunctionMiddleware` that raises `MiddlewareTermination(reason)` post-`call_next()`
    when `predicate()` returns true. Predicate is invoked after every tool dispatch."""
    return _PredicateTermination(predicate, reason)
