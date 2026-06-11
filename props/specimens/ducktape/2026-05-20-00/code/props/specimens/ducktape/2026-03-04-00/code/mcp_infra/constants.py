"""Shared constants for MCP infrastructure."""

from pathlib import Path
from typing import Final

from mcp_infra.prefix import MCPMountPrefix

# ============================================================================
# Container Filesystem Paths
# ============================================================================
WORKING_DIR: Final[Path] = Path("/workspace")

# ============================================================================
# Container Lifecycle & Process Control
# ============================================================================
SLEEP_FOREVER_CMD: Final[list[str]] = ["/bin/sh", "-lc", "sleep infinity"]

# ============================================================================
# Server Mount Prefixes - Core Infrastructure
# ============================================================================
RESOURCES_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("resources")
RUNTIME_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("runtime")
COMPOSITOR_META_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("compositor_meta")
UI_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("ui")

# ============================================================================
# Server Mount Prefixes - Approval Policy
# ============================================================================
POLICY_READER_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("policy_reader")
POLICY_PROPOSER_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("policy_proposer")
APPROVAL_ADMIN_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("approval_admin")

# ============================================================================
# Server Mount Prefixes - Optional/Specialized
# ============================================================================
SEATBELT_EXEC_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("seatbelt_exec")
