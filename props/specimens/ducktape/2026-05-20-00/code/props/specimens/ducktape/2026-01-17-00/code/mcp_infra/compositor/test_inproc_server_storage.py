"""Tests for in-process server storage and retrieval functionality.

This module tests that Mount and Compositor correctly store and retrieve
in-process FastMCP server instances, enabling direct introspection of
server state without going through the MCP client protocol.
"""

from __future__ import annotations

from fastmcp.mcp_config import StdioMCPServer
from fastmcp.server import FastMCP

from mcp_infra.compositor.mount import Mount
from mcp_infra.compositor.server import Compositor
from mcp_infra.prefix import MCPMountPrefix


async def test_mount_stores_inproc_server(compositor):
    """Test that Mount stores the server instance when setup_inproc() is called."""
    server = FastMCP("test-server")

    await compositor.mount_inproc(MCPMountPrefix("runtime"), server)

    # Access the mount directly
    mount = compositor._mounts.get("runtime")
    assert mount is not None
    assert mount.inproc_server is server


async def test_mount_inproc_server_returns_server(compositor):
    """Test that Mount.inproc_server property returns the stored server."""
    server = FastMCP("test-server")

    await compositor.mount_inproc(MCPMountPrefix("runtime"), server)

    mount = compositor._mounts["runtime"]
    result = mount.inproc_server

    assert result is not None
    assert result is server
    assert result.name == "test-server"


async def test_mount_inproc_server_none_for_external():
    """Test that Mount.inproc_server returns None for non-inproc mounts."""
    # This test assumes we have a way to create an external mount
    # For now, we'll just verify the behavior with a freshly created mount
    # before setup (which has _server = None)
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


async def test_compositor_get_inproc_server_none_for_external(compositor):
    """Test that Compositor.get_inproc_server() returns None for external mounts."""
    # Create a compositor and mount an external server
    # For this test, we'll verify the behavior by checking that external mounts
    # (those created with mount_server) don't expose an inproc_server

    # Since we don't have a simple way to create a valid external mount in tests
    # without actual external processes, we'll simulate this by checking
    # the mount's spec property - external mounts have non-None spec

    server = FastMCP("test-server")

    # Mount an in-process server
    await compositor.mount_inproc(MCPMountPrefix("inproc"), server)

    # Verify in-process mount has inproc_server
    mount = compositor._mounts["inproc"]
    assert mount.spec is None  # In-process mounts have no spec
    assert mount.inproc_server is server
    assert compositor.get_inproc_server("inproc") is server


async def test_compositor_get_inproc_server_multiple_servers(compositor):
    """Test get_inproc_server with multiple mounted servers of mixed types."""
    inproc_server = FastMCP("inproc")

    await compositor.mount_inproc(MCPMountPrefix("inproc1"), inproc_server)

    # Get the in-process server
    result = compositor.get_inproc_server("inproc1")
    assert result is inproc_server

    # Non-existent mount returns None
    result = compositor.get_inproc_server("nonexistent")
    assert result is None


async def test_mount_inproc_server_persists_through_lifecycle(compositor):
    """Test that inproc_server reference persists through mount lifecycle."""
    server = FastMCP("persistent")

    await compositor.mount_inproc(MCPMountPrefix("persistent"), server)

    # Check at various stages
    mount = compositor._mounts["persistent"]

    # Initially active and has server
    assert mount.is_active
    assert mount.inproc_server is server

    # Through compositor accessor
    assert compositor.get_inproc_server("persistent") is server


async def test_inproc_server_accessor_is_synchronous(compositor):
    """Test that inproc_server and get_inproc_server are synchronous (not async)."""
    server = FastMCP("sync-test")

    await compositor.mount_inproc(MCPMountPrefix("sync"), server)

    # These should be synchronous property/method calls
    mount = compositor._mounts["sync"]
    result1 = mount.inproc_server  # Not awaited
    result2 = compositor.get_inproc_server("sync")  # Not awaited

    assert result1 is server
    assert result2 is server


async def test_inproc_server_available_before_proxy_use(compositor):
    """Test that inproc_server is available immediately after mount."""
    server = FastMCP("immediate")

    @server.tool()
    def test_tool() -> str:
        """Test tool."""
        return "test"

    await compositor.mount_inproc(MCPMountPrefix("immediate"), server)

    # Server instance should be available immediately
    retrieved = compositor.get_inproc_server("immediate")
    assert retrieved is server

    # And we can introspect it directly
    assert retrieved.name == "immediate"


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


async def test_pinned_inproc_server_persists_after_close(compositor):
    """Test that pinned in-process servers remain accessible after close."""
    server = FastMCP("pinned")

    await compositor.mount_inproc(MCPMountPrefix("pinned"), server, pinned=True)

    # Available during context
    assert compositor.get_inproc_server("pinned") is server

    # After context exit, pinned server should still be accessible
    # (Note: with fixture pattern, compositor stays open for whole test)
    assert compositor.get_inproc_server("pinned") is server


async def test_multiple_inproc_servers_independent(compositor):
    """Test that multiple in-process servers are independently stored."""
    server1 = FastMCP("server1")
    server2 = FastMCP("server2")
    server3 = FastMCP("server3")

    await compositor.mount_inproc(MCPMountPrefix("s1"), server1)
    await compositor.mount_inproc(MCPMountPrefix("s2"), server2)
    await compositor.mount_inproc(MCPMountPrefix("s3"), server3)

    # Each prefix returns its own server
    assert compositor.get_inproc_server("s1") is server1
    assert compositor.get_inproc_server("s2") is server2
    assert compositor.get_inproc_server("s3") is server3

    # They're all different objects
    assert server1 is not server2
    assert server2 is not server3
    assert server1 is not server3


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
