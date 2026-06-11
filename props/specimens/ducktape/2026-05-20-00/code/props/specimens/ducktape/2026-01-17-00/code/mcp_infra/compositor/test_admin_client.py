from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from mcp_infra.compositor.admin import DetachServerArgs
from mcp_infra.constants import COMPOSITOR_META_MOUNT_PREFIX
from mcp_infra.prefix import MCPMountPrefix


async def test_admin_server_detach(compositor, compositor_admin_tool, make_simple_mcp):
    """Test CompositorAdminServer.detach_server() removes a mounted server."""
    # Mount backend server
    await compositor.mount_inproc(MCPMountPrefix("backend"), make_simple_mcp)

    # Verify backend is mounted
    states = await compositor.server_entries()
    assert "backend" in states

    # Detach backend via admin tool
    await compositor_admin_tool("detach_server", DetachServerArgs(prefix=MCPMountPrefix("backend")))

    # Verify backend was removed
    states_after = await compositor.server_entries()
    assert "backend" not in states_after


async def test_admin_cannot_detach_pinned_server(compositor, compositor_admin_tool):
    """Test CompositorAdminServer.detach_server() prevents detaching pinned servers."""
    # compositor_meta is mounted and pinned by default in the compositor fixture

    # Verify compositor_meta is mounted
    states_before = await compositor.server_entries()
    assert COMPOSITOR_META_MOUNT_PREFIX in states_before

    # Attempt to detach the pinned meta server via admin tool (should raise ToolError)
    with pytest.raises(ToolError, match="pinned"):
        await compositor_admin_tool("detach_server", DetachServerArgs(prefix=COMPOSITOR_META_MOUNT_PREFIX))

    # Verify meta server still present after failed detach
    states_after = await compositor.server_entries()
    assert COMPOSITOR_META_MOUNT_PREFIX in states_after
