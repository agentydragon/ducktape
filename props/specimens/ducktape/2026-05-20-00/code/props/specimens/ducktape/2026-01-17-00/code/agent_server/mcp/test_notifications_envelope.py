from __future__ import annotations

import pytest

from agent_server.notifications.handler import format_notifications_message
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.notifications import parse_system_notification_payload


@pytest.fixture
def make_notifier():
    """Factory for EnhancedFastMCP servers with emit tool."""

    def _make():
        m = EnhancedFastMCP("child")

        @m.tool(name="emit")
        async def emit():
            await m.broadcast_resource_list_changed()
            await m.broadcast_resource_updated("resource://dummy")
            return True

        return m

    return _make


@pytest.fixture
def notifier(make_notifier):
    """EnhancedFastMCP server for testing notification envelopes."""
    return make_notifier()


async def test_notifications_envelope_with_real_mcp(make_buffered_client, notifier):
    child_prefix = MCPMountPrefix("child")
    async with make_buffered_client({"child": notifier}) as (sess, _comp, buf):
        await sess.call_tool(name=build_mcp_function(child_prefix, "emit"), arguments={})
        batch = buf.poll()
        msg = format_notifications_message(batch)
        assert msg is not None
        payload = parse_system_notification_payload(msg)
        resources = payload.get("resources")
        assert isinstance(resources, dict)
        assert "child" in resources
        child_obj = resources["child"]
        assert isinstance(child_obj, dict)
        assert child_obj.get("list_changed") is True


async def test_notifications_envelope_after_remount(make_buffered_client, notifier, make_notifier):
    child_prefix = MCPMountPrefix("child")
    async with make_buffered_client({"child": notifier}) as (sess, comp, buf):
        await sess.call_tool(name=build_mcp_function(child_prefix, "emit"), arguments={})
        _ = buf.poll()

        # Unmount and re-mount a fresh notifier to simulate reconnect/new client path
        await comp.unmount_server(child_prefix)
        await comp.mount_inproc(child_prefix, make_notifier())

        await sess.call_tool(name=build_mcp_function(child_prefix, "emit"), arguments={})
        batch = buf.poll()
        msg = format_notifications_message(batch)
        assert msg is not None
        payload = parse_system_notification_payload(msg)
        resources = payload.get("resources")
        assert isinstance(resources, dict)
        assert "child" in resources
        assert resources["child"].get("list_changed") is True
