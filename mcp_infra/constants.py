"""Shared constants for MCP infrastructure."""

from pathlib import Path
from typing import Final

from mcp_infra.prefix import MCPMountPrefix

WORKING_DIR: Final[Path] = Path("/workspace")

SLEEP_FOREVER_CMD: Final[list[str]] = ["/bin/sh", "-lc", "sleep infinity"]

RESOURCES_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("resources")
COMPOSITOR_META_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("compositor_meta")
