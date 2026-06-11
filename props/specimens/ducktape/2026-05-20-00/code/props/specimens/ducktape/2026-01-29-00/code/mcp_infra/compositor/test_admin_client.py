from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_bazel
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from mcp_infra.compositor.admin import CompositorAdminServer, DetachServerArgs
from mcp_infra.constants import COMPOSITOR_META_MOUNT_PREFIX
from mcp_infra.mounted import Mounted
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastmcp import Client


@pytest.fixture
async def mounted_compositor_admin(compositor) -> Mounted[CompositorAdminServer]:
    """Mounted CompositorAdminServer for testing.

    Mounts the admin server with prefix 'test_compositor_admin' to avoid
    conflicts with any production admin server mounting logic.
    """
    admin_server = CompositorAdminServer(compositor=compositor)
    mounted: Mounted[CompositorAdminServer] = await compositor.mount_inproc(
        MCPMountPrefix("test_compositor_admin"), admin_server
    )
    return mounted


@pytest.fixture
def compositor_admin_tool(
    compositor_client: Client, mounted_compositor_admin: Mounted[CompositorAdminServer]
) -> Callable[[str, BaseModel], Awaitable]:
    """Helper to call tools on the mounted compositor admin server."""

    def call_admin_tool(tool_name: str, arguments: BaseModel):
        return compositor_client.call_tool(
            build_mcp_function(mounted_compositor_admin.prefix, tool_name), arguments.model_dump()
        )

    return call_admin_tool


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


if __name__ == "__main__":
    pytest_bazel.main()
