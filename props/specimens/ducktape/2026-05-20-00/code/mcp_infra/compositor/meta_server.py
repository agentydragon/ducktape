from __future__ import annotations

import json

from mcp_infra.compositor.server import BaseCompositor
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.mount_types import MountEvent
from mcp_infra.prefix import MCPMountPrefix

_SERVER_STATE_URI_TEMPLATE = "compositor://{server}/state"


class CompositorMetaServer(EnhancedFastMCP):
    """Compositor metadata server with typed resource access.

    Exposes mount metadata as resources on a dedicated server.
    This removes the need for synthetic mcp-server:// URIs and avoids special-casing
    in the resources aggregator.
    """

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

        # Register resources (v3 decorators return the original function, not component objects)
        @self.resource(
            "compositor://servers",
            name="compositor.servers",
            mime_type="application/json",
            description="List of all mounted servers",
        )
        async def servers_list() -> str:
            """Return list of all mounted server names for discovery."""
            entries = await self._compositor.server_entries()
            return json.dumps(list(entries.keys()))

        @self.resource(
            _SERVER_STATE_URI_TEMPLATE,
            name="compositor.state",
            mime_type="application/json",
            description="Per-server state snapshot (initializing|running|failed)",
        )
        async def server_state(server: str) -> str:
            prefix = MCPMountPrefix(server)
            entries = await self._compositor.server_entries()
            if (entry := entries.get(prefix)) is None:
                raise KeyError(server)
            return entry.model_dump_json()

        # Instructions and capabilities are embedded in the per-server state (InitializeResult)
        # via server_state above; no separate resources are exposed to avoid duplication.

        # Register mount change listener to emit notifications without container coupling
        async def _on_mount_change(name: str, action: MountEvent) -> None:
            # Always signal list-changed when mounts change
            await self.broadcast_resource_list_changed()
            # For new state availability or mount, update the per-server state resource
            if action in (MountEvent.MOUNTED, MountEvent.STATE):
                await self.broadcast_resource_updated(_SERVER_STATE_URI_TEMPLATE.format(server=name))

        self._compositor.add_mount_listener(_on_mount_change)
