from __future__ import annotations

import pytest_bazel
from fastmcp.client import Client

from agent_server.notifications.handler import format_notifications_message
from mcp_infra.compositor.notifications_buffer import NotificationsBuffer
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.notifications import parse_system_notification_payload


async def test_stdio_child_notifications_envelope(compositor, stdio_notifier_spec):
    """Test that stdio child server notifications are properly enveloped."""
    await compositor.mount_server("stdio_child", stdio_notifier_spec)

    buf = NotificationsBuffer(compositor=compositor)
    async with Client(compositor, message_handler=buf.handler) as sess:
        # Fire notifications via namespaced tool
        await sess.call_tool(name=build_mcp_function(MCPMountPrefix("stdio_child"), "emit"), arguments={})
        batch = buf.poll()
        msg = format_notifications_message(batch)
        assert msg is not None
        payload = parse_system_notification_payload(msg)
        resources = payload.get("resources")
        assert isinstance(resources, dict)
        assert "stdio_child" in resources
        child = resources["stdio_child"]
        assert isinstance(child, dict)
        # Should include list_changed and updated with the dummy URI
        assert child.get("list_changed") is True
        assert "resource://dummy" in (child.get("updated") or [])


if __name__ == "__main__":
    pytest_bazel.main()
