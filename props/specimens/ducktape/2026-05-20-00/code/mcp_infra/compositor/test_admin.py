from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from mcp_infra.compositor.admin import CompositorAdminServer, DetachServerArgs, convert_mcp_server_types_to_spec
from mcp_infra.constants import COMPOSITOR_META_MOUNT_PREFIX
from mcp_infra.mounted import Mounted
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


# --- Direct client fixtures ---


@pytest.fixture
async def admin_client(compositor):
    """Client connected directly to compositor admin server."""
    admin_server = CompositorAdminServer(compositor=compositor)
    async with Client(admin_server) as client:
        yield client


# --- Direct client tests ---


async def test_compositor_admin_attach_detach(admin_client, compositor, stdio_echo_spec):
    # Create a stdio child spec and attach
    spec = convert_mcp_server_types_to_spec(stdio_echo_spec)
    # OpenAI strict mode requires all fields including None values
    spec_dict = spec.model_dump(mode="json", exclude_none=False)
    await admin_client.call_tool("attach_server", arguments={"prefix": "backend", "spec": spec_dict})
    specs = await compositor.mount_specs()
    assert "backend" in specs

    # Detach should remove the server
    await admin_client.call_tool("detach_server", arguments={"prefix": "backend"})
    specs_after = await compositor.mount_specs()
    assert "backend" not in specs_after


async def test_compositor_admin_attach_twice_errors(admin_client, stdio_echo_spec):
    spec = convert_mcp_server_types_to_spec(stdio_echo_spec)
    spec_dict = spec.model_dump(mode="json", exclude_none=False)
    await admin_client.call_tool("attach_server", arguments={"prefix": "backend2", "spec": spec_dict})
    with pytest.raises(Exception, match=r"backend2.*already.*mounted|name.*already.*exists"):
        await admin_client.call_tool("attach_server", arguments={"prefix": "backend2", "spec": spec_dict})


async def test_compositor_admin_detach_pinned_server_fails(admin_client):
    # Attempt to detach a pinned server should raise
    with pytest.raises(Exception, match=r"pinned|cannot.*detach"):
        await admin_client.call_tool("detach_server", arguments={"prefix": COMPOSITOR_META_MOUNT_PREFIX})


async def test_compositor_admin_attach_invalid_name_errors(admin_client, stdio_echo_spec):
    # Invalid name violating pattern (uppercase not allowed)
    spec = convert_mcp_server_types_to_spec(stdio_echo_spec)
    spec_dict = spec.model_dump(mode="json", exclude_none=False)
    with pytest.raises(Exception, match=r"validation.*error|invalid.*prefix|String should match pattern"):
        await admin_client.call_tool("attach_server", arguments={"prefix": "BadName", "spec": spec_dict})


# --- Mounted client fixtures (admin server mounted on compositor) ---


@pytest.fixture
async def mounted_compositor_admin(compositor) -> Mounted[CompositorAdminServer]:
    """Mounted CompositorAdminServer for testing."""
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


# --- Mounted client tests ---


async def test_admin_server_detach(compositor, compositor_admin_tool, make_simple_mcp):
    """Test CompositorAdminServer.detach_server() removes a mounted server."""
    await compositor.mount_inproc(MCPMountPrefix("backend"), make_simple_mcp)

    states = await compositor.server_entries()
    assert "backend" in states

    await compositor_admin_tool("detach_server", DetachServerArgs(prefix=MCPMountPrefix("backend")))

    states_after = await compositor.server_entries()
    assert "backend" not in states_after


async def test_admin_cannot_detach_pinned_server(compositor, compositor_admin_tool):
    """Test CompositorAdminServer.detach_server() prevents detaching pinned servers."""
    states_before = await compositor.server_entries()
    assert COMPOSITOR_META_MOUNT_PREFIX in states_before

    with pytest.raises(ToolError, match="pinned"):
        await compositor_admin_tool("detach_server", DetachServerArgs(prefix=COMPOSITOR_META_MOUNT_PREFIX))

    states_after = await compositor.server_entries()
    assert COMPOSITOR_META_MOUNT_PREFIX in states_after


if __name__ == "__main__":
    pytest_bazel.main()
