"""MAF middleware shims shared by props agents.

`terminate_after_tools` ends the run as soon as a terminal tool fires, driven by the
`submit` / `report_failure` / `report_success` tools. Agent-specific middleware lives beside
its agent (`grader/notification_middleware.py`, `improve_termination_middleware` in
`critic_dev/main.py`). Budget is enforced server-side by props-llm-proxy, and tool
output size is bounded at the `exec` tool (`max_bytes`), so neither needs a middleware.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Set as AbstractSet

from agent_framework import FunctionInvocationContext, MiddlewareTermination, function_middleware

# Precise callable type for a function-invocation middleware. It is a member of MAF's
# `MiddlewareTypes` union, so it is accepted in an `Agent(middleware=[...])` list while
# remaining directly callable (e.g. in tests).
FunctionMiddlewareCallable = Callable[[FunctionInvocationContext, Callable[[], Awaitable[None]]], Awaitable[None]]


def terminate_after_tools(names: AbstractSet[str]) -> FunctionMiddlewareCallable:
    """Function middleware that ends the run immediately after any named terminal tool runs.

    The tool executes (its DB writes / status flips happen), then the function-calling loop
    is terminated so the model takes no further turns.
    """

    @function_middleware
    async def middleware(context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        await call_next()
        if context.function.name in names:
            raise MiddlewareTermination(f"terminal tool {context.function.name!r} called")

    return middleware
