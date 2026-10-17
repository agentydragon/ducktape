"""Prefixes for the MCP servers managed by the agent CLI compositor."""

from typing import Final

from mcp_infra.prefix import MCPMountPrefix

RESOURCES_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("resources")
COMPOSITOR_META_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("compositor_meta")
