from __future__ import annotations

import pytest
import pytest_bazel
from fastmcp.client import Client

from mcp_infra.compositor.admin import CompositorAdminServer, convert_mcp_server_types_to_spec
from mcp_infra.constants import COMPOSITOR_META_MOUNT_PREFIX


@pytest.fixture
async def admin_client(compositor):
    """Client connected to compositor admin server."""
    admin_server = CompositorAdminServer(compositor=compositor)
    async with Client(admin_server) as client:
        yield client


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


if __name__ == "__main__":
    pytest_bazel.main()
