"""Start MCP group executors together and stop them with the context."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from pydantic import ValidationError

from agentplane.action_service.catalog import ActionCatalog, McpExecutorBinding
from agentplane.action_service.mcp_executor import McpActionGroupExecutor
from agentplane.action_service.mcp_linkage import McpLinkageAuthority
from agentplane.action_service.service import Executor


@asynccontextmanager
async def running_executor(
    catalog: ActionCatalog,
    linkage: McpLinkageAuthority | None = None,
    *,
    on_health_change: Callable[[], None] | None = None,
) -> AsyncIterator[dict[str, Executor]]:
    """Validate MCP bindings locally, then schedule independent backend supervisors."""
    executors: dict[str, Executor] = {}
    supervised: dict[str, McpActionGroupExecutor] = {}
    for key, group in catalog.groups.items():
        match group.executor:
            case McpExecutorBinding():
                try:
                    supervised[key] = (
                        McpActionGroupExecutor.from_group_with_linkage(key, group, linkage)
                        if linkage is not None
                        else McpActionGroupExecutor.from_group(key, group)
                    )
                except ValidationError as error:
                    raise ValueError(f"ActionGroup {key!r} has an invalid MCP binding") from error
            case _:
                raise ValueError(f"ActionGroup {key!r} uses an executor unsupported by the Python MCP runner")

    async with AsyncExitStack() as stack:
        for mcp_executor in supervised.values():
            if on_health_change is not None:
                mcp_executor.on_health_change = on_health_change
            await stack.enter_async_context(mcp_executor)
        executors.update(supervised)
        yield executors
