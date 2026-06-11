"""Compositor with infrastructure servers (resources, compositor_meta)."""

from __future__ import annotations

from mcp_infra.compositor.meta_server import CompositorMetaServer
from mcp_infra.compositor.resources_server import ResourcesServer
from mcp_infra.compositor.server import BaseCompositor
from mcp_infra.constants import COMPOSITOR_META_MOUNT_PREFIX, RESOURCES_MOUNT_PREFIX
from mcp_infra.mounted import Mounted


class Compositor(BaseCompositor):
    """MCP server compositor with infrastructure servers.

    Extends BaseCompositor with auto-mounted infrastructure servers:
    - resources: Aggregates resources across all mounted servers
    - compositor_meta: Exposes compositor state and server metadata

    MUST be used as async context manager:
        async with Compositor() as comp:
            await comp.mount_inproc(RUNTIME_MOUNT_PREFIX, runtime_server)
            async with Client(comp) as client:
                result = await client.call_tool("runtime_exec", {"command": ["ls"]})

    Common Patterns:

    1. Short-lived script:
        async with Compositor() as comp:
            await comp.mount_inproc(RUNTIME_MOUNT_PREFIX, RuntimeServer(...))
            async with Client(comp) as client:
                agent = await Agent.create(mcp_client=client)
                await agent.run("review this code")

    2. Long-lived server:
        stack = AsyncExitStack()
        comp = Compositor()
        await stack.enter_async_context(comp)
        await comp.mount_servers_from_config(config)
        # Later: await stack.aclose() cleans up compositor

    See BaseCompositor for mount management details.
    """

    # Infrastructure servers (mounted automatically in __aenter__, always pinned)
    resources: Mounted[ResourcesServer]
    compositor_meta: Mounted[CompositorMetaServer]

    async def __aenter__(self) -> Compositor:
        """Enter context and mount infrastructure servers.

        Auto-mounts resources and compositor_meta as pinned servers.

        Raises:
            RuntimeError: If already entered or closed
        """
        await super().__aenter__()

        # Mount infrastructure servers (always pinned)
        self.resources = await self.mount_inproc(RESOURCES_MOUNT_PREFIX, ResourcesServer(compositor=self), pinned=True)
        self.compositor_meta = await self.mount_inproc(
            COMPOSITOR_META_MOUNT_PREFIX, CompositorMetaServer(compositor=self), pinned=True
        )

        return self
