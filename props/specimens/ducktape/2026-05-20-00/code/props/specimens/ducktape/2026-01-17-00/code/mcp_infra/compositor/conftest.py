"""Fixtures for compositor tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from mcp_infra.compositor.admin import CompositorAdminServer
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
