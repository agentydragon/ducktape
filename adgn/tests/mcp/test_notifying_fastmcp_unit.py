from __future__ import annotations

from fastmcp.client import Client

from adgn.mcp._shared.urls import parse_any_url
from adgn.mcp.notifications.buffer import NotificationsBuffer
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP


def _u(s: str) -> str:
    """Normalize a URI using the shared AnyUrl adapter for comparisons."""
    return str(parse_any_url(s))


async def test_queued_notifications_flush_on_first_list(make_compositor):
    # Create server and queue a list_changed before any sessions exist
    srv = NotifyingFastMCP("child")
    await srv.broadcast_resource_list_changed()  # queued (no sessions yet)

    async with make_compositor({"child": srv}) as (_sess, comp):
        buf = NotificationsBuffer(compositor=comp)
        async with Client(comp, message_handler=buf.handler) as client:
            # Trigger a server-side list (captures session and flushes pending)
            await client.session.list_resources()
            batch = buf.poll()
            # Expect child reported in list_changed (attributed by compositor)
            assert "child" in batch.resource_list_changed


async def test_broadcast_continues_after_session_failure(make_compositor):
    # Create server and open two client sessions
    srv = NotifyingFastMCP("notifier")
    async with make_compositor({"notifier": srv}) as (_sess, comp):
        buf = NotificationsBuffer(compositor=comp)
        async with Client(comp, message_handler=buf.handler) as client1:
            # Open a second client attached to the same Compositor
            async with Client(comp) as client2:
                # Prime both sessions by listing resources once
                await client1.session.list_resources()
                await client2.session.list_resources()

            # After client2 context exits, simulate broadcast with one stale session
            target_uri = _u("notifier://test")
            await srv.broadcast_resource_updated(target_uri)

            batch = buf.poll()
            # Expect at least one resources_updated event delivered to the active client
            assert any(ev.uri == target_uri for ev in batch.resources_updated), (
                "expected resources_updated for the active session despite one failing client"
            )
