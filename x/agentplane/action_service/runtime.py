"""Production composition of the reviewed ActionGroups; no dynamic adapter registry.

An MCP group gets a supervised client of its own. A sandbox group gets an executor over this
service's own Kubernetes access instead, which is why `sandboxes` has to be supplied before any
group may use that kind: a deployment that configured one and wired no clients would otherwise
start and refuse every call at dispatch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from pydantic import ValidationError

from x.agentplane.action_service.catalog import ActionCatalog, McpExecutorBinding
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor
from x.agentplane.action_service.mcp_linkage import McpLinkageAuthority
from x.agentplane.action_service.models import Executor
from x.agentplane.sandbox_actions.binding import SandboxExecutorBinding
from x.agentplane.sandbox_actions.executor import SandboxExecutor, actions
from x.agentplane.sandbox_actions.inventory import SandboxClients


@asynccontextmanager
async def running_executor(
    catalog: ActionCatalog, linkage: McpLinkageAuthority | None = None, sandboxes: SandboxClients | None = None
) -> AsyncIterator[dict[str, Executor]]:
    """Validate bindings locally, then schedule independent optional backend supervisors."""
    executors: dict[str, Executor] = {}
    supervised: dict[str, McpActionGroupExecutor] = {}
    for key, group in catalog.groups.items():
        match group.executor:
            case SandboxExecutorBinding() as binding:
                if sandboxes is None:
                    raise ValueError(f"ActionGroup {key!r} is a sandbox group and no Kubernetes access was supplied")
                # Declared, not discovered: a code-owned group's roster is its own models, so it is
                # offered from the moment configuration validates rather than after a handshake.
                group.actions = actions(binding)
                executors[key] = SandboxExecutor(binding, sandboxes.inventory(binding))
            case McpExecutorBinding():
                try:
                    supervised[key] = (
                        McpActionGroupExecutor.from_group_with_linkage(key, group, linkage)
                        if linkage is not None
                        else McpActionGroupExecutor.from_group(key, group)
                    )
                except ValidationError:
                    # Pydantic's default exception text includes raw binding values.
                    raise ValueError(f"ActionGroup {key!r} has an invalid MCP binding") from None

    async with AsyncExitStack() as stack:
        for key, mcp_executor in supervised.items():
            # Register before start: a connected client can fail during initial discovery.
            stack.push_async_callback(_close_executor, key, mcp_executor)
            await mcp_executor.start()
        yield {**executors, **supervised}


async def _close_executor(key: str, executor: McpActionGroupExecutor) -> None:
    try:
        await executor.close()
    except Exception:
        # The pinned HTTP client can re-raise a terminal transport failure during shutdown.
        raise RuntimeError(f"ActionGroup {key!r} MCP shutdown failed") from None
