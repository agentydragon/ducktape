from __future__ import annotations

from tests.util.notifications import parse_system_notification_payload

from adgn.agent.reducer import format_notifications_message
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP


def _make_notifier(name: str = "child") -> NotifyingFastMCP:
    m = NotifyingFastMCP(name)

    # Define a simple tool that emits list_changed and a dummy updated
    @m.tool(name="emit")
    async def emit():
        # Emit a list_changed and one updated for a fixed URI
        await m.broadcast_resource_list_changed()
        await m.broadcast_resource_updated("resource://dummy")
        return True

    return m


async def test_notifications_envelope_with_real_mcp(make_buffered_client):
    child = _make_notifier("child")
    async with make_buffered_client({"child": child}) as (sess, _comp, buf):
        await sess.call_tool(name=build_mcp_function("child", "emit"), arguments={})
        batch = buf.poll()
        # Format and parse the system notification payload
        msg = format_notifications_message(batch)
        assert msg is not None
        payload = parse_system_notification_payload(msg)
        # Expect grouped resources mapping with the child listed (list_changed best-effort)
        resources = payload.get("resources")
        assert isinstance(resources, dict)
        # At minimum, list_changed should flag the child server
        assert "child" in resources
        child_obj = resources["child"]
        assert isinstance(child_obj, dict)
        assert child_obj.get("list_changed") is True


async def test_notifications_envelope_after_remount(make_buffered_client):
    child = _make_notifier("child")
    async with make_buffered_client({"child": child}) as (sess, comp, buf):
        await sess.call_tool(name=build_mcp_function("child", "emit"), arguments={})
        _ = buf.poll()

        # Unmount and re-mount a fresh notifier to simulate reconnect/new client path
        await comp.unmount_server("child")
        await comp.mount_inproc("child", _make_notifier("child"))

        await sess.call_tool(name=build_mcp_function("child", "emit"), arguments={})
        batch = buf.poll()
        msg = format_notifications_message(batch)
        assert msg is not None
        payload = parse_system_notification_payload(msg)
        resources = payload.get("resources")
        assert isinstance(resources, dict)
        assert "child" in resources
        assert resources["child"].get("list_changed") is True
