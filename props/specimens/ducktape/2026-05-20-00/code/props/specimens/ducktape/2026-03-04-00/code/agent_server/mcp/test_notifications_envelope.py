from __future__ import annotations

import pytest
import pytest_bazel
from fastmcp.client import Client

from agent_server.notifications.handler import format_notifications_message
from mcp_infra.compositor.notifications_buffer import NotificationsBuffer
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.notifications import parse_system_notification_payload


def _make_inproc_notifier() -> EnhancedFastMCP:
    m = EnhancedFastMCP("child")

    @m.tool(name="emit")
    async def emit():
        await m.broadcast_resource_list_changed()
        await m.broadcast_resource_updated("resource://dummy")
        return True

    return m


CHILD_PREFIX = MCPMountPrefix("child")


@pytest.fixture
def child_prefix():
    return CHILD_PREFIX


@pytest.fixture
def notifications_buffer(compositor):
    return NotificationsBuffer(compositor=compositor)


@pytest.fixture(params=["inproc", "stdio"])
async def envelope_session(request, compositor, notifications_buffer, child_prefix, stdio_notifier_spec):
    """Yield an MCP client session with a notifier child mounted (inproc or stdio)."""
    if request.param == "inproc":
        await compositor.mount_inproc(child_prefix, _make_inproc_notifier())
    else:
        await compositor.mount_server(child_prefix, stdio_notifier_spec)

    async with Client(compositor, message_handler=notifications_buffer.handler) as sess:
        yield sess


async def test_notifications_envelope(envelope_session, notifications_buffer, child_prefix):
    await envelope_session.call_tool(name=build_mcp_function(child_prefix, "emit"), arguments={})
    batch = notifications_buffer.poll()
    msg = format_notifications_message(batch)
    assert msg is not None
    payload = parse_system_notification_payload(msg)
    resources = payload.get("resources")
    assert isinstance(resources, dict)
    assert "child" in resources
    child_obj = resources["child"]
    assert isinstance(child_obj, dict)
    assert child_obj.get("list_changed") is True
    assert "resource://dummy" in (child_obj.get("updated") or [])


async def test_notifications_envelope_after_remount(compositor, notifications_buffer, child_prefix):
    """Remount-specific test: notifications work after unmount/remount cycle."""
    await compositor.mount_inproc(child_prefix, _make_inproc_notifier())

    async with Client(compositor, message_handler=notifications_buffer.handler) as sess:
        await sess.call_tool(name=build_mcp_function(child_prefix, "emit"), arguments={})
        _ = notifications_buffer.poll()

        await compositor.unmount_server(child_prefix)
        await compositor.mount_inproc(child_prefix, _make_inproc_notifier())

        await sess.call_tool(name=build_mcp_function(child_prefix, "emit"), arguments={})
        batch = notifications_buffer.poll()
        msg = format_notifications_message(batch)
        assert msg is not None
        payload = parse_system_notification_payload(msg)
        resources = payload.get("resources")
        assert isinstance(resources, dict)
        assert "child" in resources
        assert resources["child"].get("list_changed") is True


if __name__ == "__main__":
    pytest_bazel.main()
