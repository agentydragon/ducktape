"""MAF middleware shims for props agents.

The only cross-cutting control behavior props needs at the middleware layer is
"end the run as soon as a terminal tool fires" — the faithful port of the old
agent_core terminal-handler behavior driven by the `submit` / `report_failure` /
`report_success` tools. Budget is enforced server-side by props-llm-proxy, and tool
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
    is terminated so the model takes no further turns — equivalent to agent_core aborting on
    the next `on_before_sample` once `exit_state` was set.
    """

    @function_middleware
    async def middleware(context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        await call_next()
        if context.function.name in names:
            raise MiddlewareTermination(f"terminal tool {context.function.name!r} called")

    return middleware
