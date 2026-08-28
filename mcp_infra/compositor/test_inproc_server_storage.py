"""Tests for in-process server storage and retrieval functionality.

This module tests that Mount and Compositor correctly store and retrieve
in-process FastMCP server instances, enabling direct introspection of
server state without going through the MCP client protocol.
"""

from __future__ import annotations

import pytest_bazel
from fastmcp.mcp_config import StdioMCPServer
from fastmcp.server import FastMCP

from mcp_infra.compositor.compositor import Compositor
from mcp_infra.compositor.mount import Mount
from mcp_infra.prefix import MCPMountPrefix


async def test_mount_stores_inproc_server(compositor):
    """Test that Mount stores the server instance when setup_inproc() is called."""
    server = FastMCP("test-server")

    await compositor.mount_inproc(MCPMountPrefix("runtime"), server)

    # Access the mount directly
    mount = compositor._mounts.get("runtime")
    assert mount is not None
    assert mount.inproc_server is server


async def test_mount_inproc_server_none_for_external():
    """Test that Mount.inproc_server returns None for non-inproc mounts."""
    mount = Mount(prefix=MCPMountPrefix("external"), pinned=False, spec=None)
    assert mount.inproc_server is None


async def test_compositor_get_inproc_server_returns_server(compositor):
    """Test that Compositor.get_inproc_server() returns the correct server."""
    server1 = FastMCP("server1")
    server2 = FastMCP("server2")

    await compositor.mount_inproc(MCPMountPrefix("runtime"), server1)
    await compositor.mount_inproc(MCPMountPrefix("docker"), server2)

    # Get server by prefix
    result1 = compositor.get_inproc_server("runtime")
    result2 = compositor.get_inproc_server("docker")

    assert result1 is server1
    assert result2 is server2


async def test_compositor_get_inproc_server_none_for_nonexistent(compositor):
    """Test that Compositor.get_inproc_server() returns None for non-existent prefix."""
    result = compositor.get_inproc_server("nonexistent")
    assert result is None


async def test_compositor_get_inproc_server_after_unmount(compositor):
    """Test that get_inproc_server returns None after unmount."""
    temp_prefix = MCPMountPrefix("temp")
    server = FastMCP("temp")

    await compositor.mount_inproc(temp_prefix, server)

    # Initially available
    assert compositor.get_inproc_server(temp_prefix) is server

    # After unmount, should return None
    await compositor.unmount_server(temp_prefix)
    assert compositor.get_inproc_server(temp_prefix) is None


async def test_compositor_get_inproc_servers_returns_all(compositor):
    """Test that get_inproc_servers() returns all mounted in-process servers."""
    server1 = FastMCP("server1")
    server2 = FastMCP("server2")
    server3 = FastMCP("server3")

    await compositor.mount_inproc(MCPMountPrefix("s1"), server1)
    await compositor.mount_inproc(MCPMountPrefix("s2"), server2)
    await compositor.mount_inproc(MCPMountPrefix("s3"), server3)

    servers = await compositor.get_inproc_servers()

    # Should include our three servers plus infrastructure servers (resources, compositor_meta)
    assert len(servers) >= 3
    assert servers[MCPMountPrefix("s1")] is server1
    assert servers[MCPMountPrefix("s2")] is server2
    assert servers[MCPMountPrefix("s3")] is server3
    # Infrastructure servers are also present
    assert MCPMountPrefix("resources") in servers
    assert MCPMountPrefix("compositor_meta") in servers


async def test_compositor_get_inproc_servers_empty(compositor):
    """Test that get_inproc_servers() returns infrastructure servers (not empty)."""
    servers = await compositor.get_inproc_servers()
    # Compositor always has infrastructure servers (resources, compositor_meta)
    assert MCPMountPrefix("resources") in servers
    assert MCPMountPrefix("compositor_meta") in servers
    # At minimum these two servers
    assert len(servers) >= 2


async def test_compositor_get_inproc_servers_excludes_external():
    """Test that get_inproc_servers() excludes external (non-inproc) mounts."""
    async with Compositor() as comp:
        # Mount one in-process server
        inproc_server = FastMCP("inproc")
        await comp.mount_inproc(MCPMountPrefix("inproc"), inproc_server)

        # Simulate an external mount by creating a mount with spec
        # (Real external mounts would go through mount_server, but that requires actual servers)
        external_spec = StdioMCPServer(command="dummy", args=[])
        external_mount = Mount(prefix=MCPMountPrefix("external"), pinned=False, spec=external_spec)
        async with comp._mount_lock:
            comp._mounts[MCPMountPrefix("external")] = external_mount

        servers = await comp.get_inproc_servers()

        # Should include in-process server plus infrastructure servers, but NOT external mount
        assert MCPMountPrefix("inproc") in servers
        assert servers[MCPMountPrefix("inproc")] is inproc_server
        assert MCPMountPrefix("external") not in servers
        # Infrastructure servers should be present
        assert MCPMountPrefix("resources") in servers
        assert MCPMountPrefix("compositor_meta") in servers


if __name__ == "__main__":
    pytest_bazel.main()
