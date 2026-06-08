"""MAF middleware shims for props agents.

The only cross-cutting control behavior props needs at the middleware layer is
"end the run as soon as a terminal tool fires" — the faithful port of agent_core's
`AbortIf(lambda: exit_state.should_exit)` driven by the `submit` / `report_failure` /
`report_success` tools. Budget is enforced server-side by props-llm-proxy, and tool
output size is bounded at the `exec` tool (`max_bytes`), so neither needs a middleware.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Set as AbstractSet

from agent_framework import FunctionInvocationContext, MiddlewareTermination, MiddlewareTypes


def terminate_after_tools(names: AbstractSet[str]) -> MiddlewareTypes:
    """Function middleware that ends the run immediately after any named terminal tool runs.

    The tool executes (its DB writes / status flips happen), then the function-calling loop
    is terminated so the model takes no further turns — equivalent to agent_core aborting on
    the next `on_before_sample` once `exit_state` was set.
    """

    async def middleware(context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        await call_next()
        if context.function.name in names:
            raise MiddlewareTermination(f"terminal tool {context.function.name!r} called")

    return middleware
