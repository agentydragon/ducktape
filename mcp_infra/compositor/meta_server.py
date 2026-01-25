from __future__ import annotations

from typing import cast

from fastmcp.resources import FunctionResource, ResourceTemplate

from mcp_infra.compositor.server import BaseCompositor
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.mount_types import MountEvent
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.snapshots import ServerEntry


class CompositorMetaServer(EnhancedFastMCP):
    """Compositor metadata server with typed resource access.

    Exposes mount metadata as resources on a dedicated server.
    This removes the need for synthetic mcp-server:// URIs and avoids special-casing
    in the resources aggregator.
    """

    # Resource attributes (stashed results of @resource decorator - single source of truth for URI access)
    servers_list_resource: FunctionResource
    server_state_resource: ResourceTemplate

    def __init__(self, *, compositor: BaseCompositor):
        """Create compositor metadata server.

        Args:
            compositor: BaseCompositor instance to expose metadata for
        """
        # Pass explicit version to avoid importlib.metadata.version() lookup which can hang under pytest-xdist
        super().__init__(
            name="Compositor Meta Server",
            version="1.0.0",
            instructions=(
                "Compositor metadata server exposing state and configuration of all mounted MCP servers.\n\n"
                "**What it provides:**\n"
                "- List of all mounted servers (resource: resource://compositor_meta/servers)\n"
                "- Per-server state snapshots (initializing, running, or failed)\n"
                "- Server capabilities (tools, resources, prompts, logging support)\n"
                "- Server-provided instructions for how to use their tools/resources\n\n"
                "**Use this to:**\n"
                "- Discover what servers are available and their current state\n"
                "- Read server-specific instructions before using their tools\n"
                "- Check capabilities to understand what features each server supports\n"
                "- Monitor server health (detect failed mounts, view error messages)\n\n"
                "Resources follow the pattern `resource://compositor_meta/state/{server}` for per-server state."
            ),
        )

        self._compositor = compositor

        # Register resources and stash the results
        async def servers_list() -> list[str]:
            """Return list of all mounted server names for discovery."""
            entries = await self._compositor.server_entries()
            return list(entries.keys())

        self.servers_list_resource = cast(
            FunctionResource,
            self.resource(
                "compositor://servers",
                name="compositor.servers",
                mime_type="application/json",
                description="List of all mounted servers",
            )(servers_list),
        )

        async def server_state(server: str) -> ServerEntry:
            prefix = MCPMountPrefix(server)
            entries = await self._compositor.server_entries()
            if (entry := entries.get(prefix)) is None:
                raise KeyError(server)
            return entry

        self.server_state_resource = cast(
            ResourceTemplate,
            self.resource(
                "compositor://{server}/state",
                name="compositor.state",
                mime_type="application/json",
                description="Per-server state snapshot (initializing|running|failed)",
            )(server_state),
        )

        # Instructions and capabilities are embedded in the per-server state (InitializeResult)
        # via server_state above; no separate resources are exposed to avoid duplication.

        # Register mount change listener to emit notifications without container coupling
        async def _on_mount_change(name: str, action: MountEvent) -> None:
            # Always signal list-changed when mounts change
            await self.broadcast_resource_list_changed()
            # For new state availability or mount, update the per-server state resource
            if action in (MountEvent.MOUNTED, MountEvent.STATE):
                await self.broadcast_resource_updated(self.server_state_resource.uri_template.format(server=name))

        self._compositor.add_mount_listener(_on_mount_change)
