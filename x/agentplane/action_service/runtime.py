"""Production composition of the reviewed MCP ActionGroups; no dynamic adapter registry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from pydantic import ValidationError

from x.agentplane.action_service.catalog import ActionCatalog, McpExecutorBinding
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor
from x.agentplane.action_service.mcp_linkage import McpLinkageAuthority
from x.agentplane.action_service.models import Executor


@asynccontextmanager
async def running_executor(
    catalog: ActionCatalog, linkage: McpLinkageAuthority | None = None
) -> AsyncIterator[dict[str, Executor]]:
    """Validate all bindings before connecting, then start each executor.

    An empty catalog intentionally serves no actions. A configured group must have a valid MCP
    binding; there is no EchoExecutor fallback. Credentialless groups must connect and discover
    successfully before serving. OAuth-linked groups may start unavailable while unlinked and
    reconnect in the background when their shared linkage authority becomes linked. Later
    discovery failures retain the adapter's unavailable-and-retry behavior.
    """
    executors: dict[str, McpActionGroupExecutor] = {}
    for key, group in catalog.groups.items():
        if not isinstance(group.executor, McpExecutorBinding):
            raise ValueError(f"ActionGroup {key!r} has an unsupported executor kind; expected 'mcp'")
        try:
            executors[key] = (
                McpActionGroupExecutor.from_group_with_linkage(key, group, linkage)
                if linkage is not None
                else McpActionGroupExecutor.from_group(key, group)
            )
        except ValidationError:
            # Pydantic's default exception text includes raw binding values.
            raise ValueError(f"ActionGroup {key!r} has an invalid MCP binding") from None

    async with AsyncExitStack() as stack:
        for key, executor in executors.items():
            # Register before start: a connected client can fail during initial discovery.
            stack.push_async_callback(_close_executor, key, executor)
            try:
                await executor.start()
            except Exception:
                # Transport exceptions can include the endpoint and backend response body.
                raise RuntimeError(f"ActionGroup {key!r} failed initial MCP discovery") from None
            if not catalog.groups[key].available and not executor.requires_linkage:
                raise RuntimeError(f"ActionGroup {key!r} failed initial MCP discovery")
        yield dict(executors)


async def _close_executor(key: str, executor: McpActionGroupExecutor) -> None:
    try:
        await executor.close()
    except Exception:
        # The pinned HTTP client can re-raise a terminal transport failure during shutdown.
        raise RuntimeError(f"ActionGroup {key!r} MCP shutdown failed") from None
