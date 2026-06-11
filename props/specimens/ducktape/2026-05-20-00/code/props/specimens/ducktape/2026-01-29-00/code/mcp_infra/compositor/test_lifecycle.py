"""Tests for Compositor lifecycle management and cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import pytest_bazel
from fastmcp.server import FastMCP

from mcp_infra.compositor.compositor import Compositor
from mcp_infra.compositor.mount import Mount, MountState
from mcp_infra.compositor.server import CompositorState
from mcp_infra.prefix import MCPMountPrefix


async def test_compositor_state_transitions():
    """Test that compositor follows correct state transitions."""
    comp = Compositor()

    # Initial state: CREATED
    assert comp._state == CompositorState.CREATED

    # Enter context: CREATED → ACTIVE
    async with comp:
        assert comp._state == CompositorState.ACTIVE

    # Exit context: ACTIVE → CLOSED
    assert comp._state == CompositorState.CLOSED


async def test_double_enter_raises():
    """Test that entering compositor twice raises RuntimeError."""
    async with Compositor() as comp:
        # Try to enter again while already active
        with pytest.raises(RuntimeError, match="already in an active context"):
            async with comp:
                pass


async def test_reuse_closed_compositor_raises():
    """Test that reusing closed compositor raises RuntimeError."""
    comp = Compositor()

    async with comp:
        pass

    # Try to reuse closed compositor
    with pytest.raises(RuntimeError, match="already closed"):
        async with comp:
            pass


async def test_mount_after_close_raises():
    """Test that mounting after close raises RuntimeError."""
    comp = Compositor()

    async with comp:
        pass

    # Try to mount after close
    server = FastMCP("backend")
    with pytest.raises(RuntimeError, match=r"compositor .* is closed"):
        await comp.mount_inproc(MCPMountPrefix("backend"), server)


async def test_cleanup_removes_non_pinned_servers():
    """Test that close() removes non-pinned servers, but __aexit__() removes all."""
    backend1 = FastMCP("backend1")
    backend2 = FastMCP("backend2")
    pinned = FastMCP("pinned")

    # Mount prefixes for dict access
    pinned_prefix = MCPMountPrefix("pinned")

    # Note: Using manual Compositor() instead of fixture because this test needs to
    # verify state AFTER __aexit__(), which happens during fixture teardown
    async with Compositor() as comp:
        await comp.mount_inproc(MCPMountPrefix("backend1"), backend1)
        await comp.mount_inproc(MCPMountPrefix("backend2"), backend2)
        await comp.mount_inproc(MCPMountPrefix("pinned"), pinned, pinned=True)

        # Before close: all mounted
        entries = await comp.server_entries()
        assert "backend1" in entries
        assert "backend2" in entries
        assert "pinned" in entries

        # Call close() explicitly (not __aexit__)
        await comp.close()

        # After close(): non-pinned removed, pinned remains
        entries = await comp.server_entries()
        assert "backend1" not in entries
        assert "backend2" not in entries
        assert "pinned" in entries

        # Verify pinned server is still active after close()
        mount = comp._mounts.get(pinned_prefix)
        assert mount is not None
        assert mount.is_active

    # After __aexit__: ALL servers cleaned up (including pinned)
    entries = await comp.server_entries()
    assert "backend1" not in entries
    assert "backend2" not in entries
    assert "pinned" not in entries  # Now cleaned up

    # Pinned server should be closed after __aexit__
    pinned_mount = comp._mounts.get(pinned_prefix)
    assert pinned_mount is None or not pinned_mount.is_active


async def test_mount_state_transitions(compositor, make_simple_mcp):
    """Test that mounts follow correct state transitions."""
    backend = make_simple_mcp

    # Mount prefix for dict access
    backend_prefix = MCPMountPrefix("backend")

    await compositor.mount_inproc(MCPMountPrefix("backend"), backend)

    mount = compositor._mounts[backend_prefix]
    # After successful mount: ACTIVE
    assert mount.state == MountState.ACTIVE
    assert mount.is_active
    assert not mount.is_failed
    assert not mount.is_closed

    # Unmount
    await compositor.unmount_server(backend_prefix)

    # After unmount: CLOSED
    assert mount.state == MountState.CLOSED
    assert not mount.is_active
    assert mount.is_closed


async def test_mount_cleanup_is_idempotent(compositor, make_simple_mcp):
    """Test that mount cleanup can be called multiple times safely."""
    backend = make_simple_mcp

    await compositor.mount_inproc(MCPMountPrefix("backend"), backend)
    mount = compositor._mounts["backend"]

    # First cleanup
    await mount.cleanup()
    assert mount.is_closed

    # Second cleanup (should not raise)
    await mount.cleanup()
    assert mount.is_closed

    # Third cleanup (should not raise)
    await mount.cleanup()
    assert mount.is_closed


async def test_accessing_inactive_mount_raises(compositor, make_simple_mcp):
    """Test that accessing proxy/client on inactive mount raises."""
    backend = make_simple_mcp

    await compositor.mount_inproc(MCPMountPrefix("backend"), backend)
    mount = compositor._mounts["backend"]

    # Before cleanup: accessible
    proxy = mount.proxy
    client = mount.child_client
    assert proxy is not None
    assert client is not None

    # Cleanup
    await mount.cleanup()

    # After cleanup: raises
    with pytest.raises(RuntimeError, match="not active"):
        _ = mount.proxy

    with pytest.raises(RuntimeError, match="not active"):
        _ = mount.child_client


async def test_exception_in_body_still_cleans_up(make_simple_mcp):
    """Test that exceptions in context body still trigger cleanup."""
    backend = make_simple_mcp

    comp = Compositor()
    try:
        async with comp:
            await comp.mount_inproc(MCPMountPrefix("backend"), backend)

            # Before exception: mounted
            entries = await comp.server_entries()
            assert "backend" in entries

            # Raise exception
            raise ValueError("test exception")
    except ValueError:
        pass

    # After exception: cleaned up
    assert comp._state == CompositorState.CLOSED
    entries = await comp.server_entries()
    assert "backend" not in entries


async def test_mount_failure_does_not_leak(compositor):
    """Test that failed mounts don't leak resources."""
    # Create a server that will fail to initialize

    failing_server = FastMCP("failing")

    # Mock the Mount.setup_inproc to simulate failure
    async def failing_setup(self, server, handler_factory=None):
        raise RuntimeError("Simulated mount failure")

    with (
        patch.object(Mount, "setup_inproc", failing_setup),
        pytest.raises(RuntimeError, match="Simulated mount failure"),
    ):
        # Mount should fail
        await compositor.mount_inproc(MCPMountPrefix("failing"), failing_server)

    # Server should NOT be registered
    entries = await compositor.server_entries()
    assert "failing" not in entries
    assert "failing" not in compositor._mounts


async def test_close_continues_on_per_server_failure(make_simple_mcp):
    """Test that close() continues cleanup even if one server fails."""
    backend1 = make_simple_mcp
    backend2 = make_simple_mcp

    async with Compositor() as comp:
        await comp.mount_inproc(MCPMountPrefix("backend1"), backend1)
        await comp.mount_inproc(MCPMountPrefix("backend2"), backend2)

        # Break one mount's cleanup
        mount1 = comp._mounts[MCPMountPrefix("backend1")]

        async def failing_cleanup():
            raise RuntimeError("Simulated cleanup failure")

        mount1.cleanup = failing_cleanup  # type: ignore[method-assign]

        # Store initial state
        entries_before = await comp.server_entries()
        assert "backend1" in entries_before
        assert "backend2" in entries_before

    # After close: backend2 should still be cleaned up despite backend1 failure
    # (backend1 will fail cleanup but be removed from dict)
    entries_after = await comp.server_entries()
    assert "backend2" not in entries_after
    # Both should be gone from dict
    assert "backend1" not in comp._mounts
    assert "backend2" not in comp._mounts


async def test_concurrent_mount_operations_safe(compositor, make_simple_mcp):
    """Test that concurrent mount operations don't corrupt state."""

    async def mount_many(comp, prefix, count):
        """Mount multiple servers concurrently."""
        tasks = []
        for i in range(count):
            name = MCPMountPrefix(f"{prefix}_{i}")
            server = make_simple_mcp
            tasks.append(comp.mount_inproc(name, server))
        await asyncio.gather(*tasks)

    # Mount many servers concurrently
    await asyncio.gather(mount_many(compositor, "group_a", 5), mount_many(compositor, "group_b", 5))

    # All should be mounted
    entries = await compositor.server_entries()
    for i in range(5):
        assert f"group_a_{i}" in entries
        assert f"group_b_{i}" in entries


async def test_get_child_client_validates_state(compositor, make_simple_mcp):
    """Test that get_child_client validates mount state."""
    backend = make_simple_mcp

    await compositor.mount_inproc(MCPMountPrefix("backend"), backend)

    # Active mount: returns client
    client = compositor.get_child_client("backend")
    assert client is not None

    # Unmount
    await compositor.unmount_server(MCPMountPrefix("backend"))

    # Inactive mount: raises
    with pytest.raises(ValueError, match="not mounted"):
        compositor.get_child_client("backend")


async def test_pinned_server_survives_close(make_simple_mcp):
    """Test that pinned servers survive close() but not __aexit__()."""
    pinned = make_simple_mcp

    # Mount prefix for dict access
    pinned_prefix = MCPMountPrefix("pinned")

    # Note: Using manual Compositor() instead of fixture because this test needs to
    # verify state AFTER __aexit__(), which happens during fixture teardown
    async with Compositor() as comp:
        await comp.mount_inproc(MCPMountPrefix("pinned"), pinned, pinned=True)

        # Verify mounted
        entries = await comp.server_entries()
        assert "pinned" in entries
        mount = comp._mounts[pinned_prefix]
        assert mount.is_active

        # Call close() explicitly
        await comp.close()

        # After close(): pinned server still active
        entries = await comp.server_entries()
        assert "pinned" in entries
        mount = comp._mounts[pinned_prefix]
        assert mount.is_active
        assert not mount.is_closed

    # After __aexit__(): pinned server is cleaned up
    entries = await comp.server_entries()
    assert "pinned" not in entries
    # Mount should be closed or removed
    assert pinned_prefix not in comp._mounts or not comp._mounts[pinned_prefix].is_active


async def test_compositor_warns_on_leak(make_simple_mcp):
    """Test that __del__ detects leaked compositors.

    Note: The actual warning emission is tested manually as pytest's warning
    capture doesn't work reliably with ResourceWarnings from __del__.
    This test verifies the leak detection logic is correct.
    """
    backend = make_simple_mcp

    # Create compositor without context manager
    comp = Compositor()
    await comp.mount_inproc(MCPMountPrefix("backend"), backend)

    # Verify the compositor is in a state that would trigger a warning
    assert comp._state == CompositorState.CREATED  # Never entered context
    assert len([n for n, m in comp._mounts.items() if not m.pinned]) > 0  # Has non-pinned mounts

    # Mock warnings.warn to verify it would be called
    with patch("warnings.warn") as mock_warn:
        # Trigger __del__ manually
        comp.__del__()

        # Verify warning was called
        assert mock_warn.called
        call_args = mock_warn.call_args
        warning_msg = call_args[0][0]
        warning_category = call_args[0][1]

        assert "COMPOSITOR LEAK" in warning_msg
        assert "backend" in warning_msg
        assert "was never used as context manager" in warning_msg
        assert warning_category is ResourceWarning


if __name__ == "__main__":
    pytest_bazel.main()
