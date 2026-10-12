"""Production composition of the reviewed ActionGroups; no dynamic adapter registry.

An MCP group gets a supervised client of its own. A sandbox group gets an executor over this
service's own Kubernetes access instead, which is why `sandboxes` has to be supplied before any
group may use that kind: a deployment that configured one and wired no clients would otherwise
start and refuse every call at dispatch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from pydantic import ValidationError

from agentplane.action_service.catalog import ActionCatalog, McpExecutorBinding
from agentplane.action_service.mcp_executor import McpActionGroupExecutor
from agentplane.action_service.mcp_linkage import McpLinkageAuthority
from agentplane.action_service.sandbox.actions import actions
from agentplane.action_service.sandbox.binding import BoxExecutorBinding, SandboxExecutorBinding
from agentplane.action_service.sandbox.clients import SandboxClients
from agentplane.action_service.sandbox.executor import SandboxExecutor
from agentplane.action_service.service import Executor


@asynccontextmanager
async def running_executor(
    catalog: ActionCatalog,
    linkage: McpLinkageAuthority | None = None,
    sandboxes: SandboxClients | None = None,
    *,
    on_health_change: Callable[[], None] | None = None,
) -> AsyncIterator[dict[str, Executor]]:
    """Validate bindings locally, then schedule independent optional backend supervisors."""
    executors: dict[str, Executor] = {}
    supervised: dict[str, McpActionGroupExecutor] = {}
    for key, group in catalog.groups.items():
        match group.executor:
            case SandboxExecutorBinding() as binding:
                if sandboxes is None:
                    raise ValueError(f"ActionGroup {key!r} is a sandbox group and no Kubernetes access was supplied")
                # Declared, not discovered: a code-owned group's roster is its own models. Only what
                # its offered templates say of themselves is read from the cluster, once, here.
                inventory = sandboxes.inventory(binding)
                group.actions = actions(binding, await inventory.template_descriptions())
                # A discovered catalog can only show a missing name later; a declared roster shows a typo now.
                if group.direct_tools is not None and (unoffered := group.direct_tools - group.actions.keys()):
                    raise ValueError(
                        f"ActionGroup {key!r} direct_tools names Actions it does not offer: {sorted(unoffered)}"
                    )
                executors[key] = SandboxExecutor(binding, inventory)
            case BoxExecutorBinding() as binding:
                if sandboxes is None:
                    raise ValueError(f"ActionGroup {key!r} is a box group and no Kubernetes access was supplied")
                # Declared, not discovered, likewise. Its offered templates are read once, here, to
                # refuse one that is missing or whose box would escape the egress path.
                box_inventory = sandboxes.box_inventory(binding)
                await box_inventory.check_templates()
                group.actions = actions(binding, binding.templates)
                if group.direct_tools is not None and (unoffered := group.direct_tools - group.actions.keys()):
                    raise ValueError(
                        f"ActionGroup {key!r} direct_tools names Actions it does not offer: {sorted(unoffered)}"
                    )
                executors[key] = SandboxExecutor(binding, box_inventory)
            case McpExecutorBinding():
                try:
                    supervised[key] = (
                        McpActionGroupExecutor.from_group_with_linkage(key, group, linkage)
                        if linkage is not None
                        else McpActionGroupExecutor.from_group(key, group)
                    )
                except ValidationError as error:
                    raise ValueError(f"ActionGroup {key!r} has an invalid MCP binding") from error

    async with AsyncExitStack() as stack:
        for mcp_executor in supervised.values():
            if on_health_change is not None:
                mcp_executor.on_health_change = on_health_change
            await stack.enter_async_context(mcp_executor)
        yield {**executors, **supervised}
