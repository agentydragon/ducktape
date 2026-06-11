from __future__ import annotations

import anyio
from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from mcp import types

from mcp_infra.enhanced.server import EnhancedFastMCP


class _Recorder(MessageHandler):
    def __init__(self) -> None:
        self.updated: list[str] = []
        self.list_changed: int = 0
        self._evt_updated = anyio.Event()
        self._evt_list = anyio.Event()

    async def on_resource_updated(self, message: types.ResourceUpdatedNotification) -> None:
        # Record and signal
        try:
            uri = str(message.params.uri)
        except Exception:
            uri = "<invalid>"
        self.updated.append(uri)
        self._evt_updated.set()

    async def on_resource_list_changed(self, message: types.ResourceListChangedNotification) -> None:
        self.list_changed += 1
        self._evt_list.set()

    async def wait_updated(self) -> None:
        with anyio.fail_after(2.0):
            await self._evt_updated.wait()

    async def wait_list_changed(self) -> None:
        with anyio.fail_after(2.0):
            await self._evt_list.wait()


async def test_notifying_fastmcp_queue_and_flush() -> None:
    # Create server but no sessions yet
    server = EnhancedFastMCP("notify_test")

    # Broadcast notifications before any session exists (should be queued)
    await server.broadcast_resource_updated("resource://foo")
    await server.broadcast_resource_list_changed()

    # Now create a client; queued events should flush upon session creation
    rec = _Recorder()
    async with Client(server, message_handler=rec) as _c:
        await rec.wait_updated()
        await rec.wait_list_changed()

    # Validate received notifications
    assert "resource://foo" in rec.updated
    assert rec.list_changed >= 1


async def test_notifying_fastmcp_multisession_broadcast() -> None:
    server = EnhancedFastMCP("notify_multi")

    rec1 = _Recorder()
    rec2 = _Recorder()

    async with Client(server, message_handler=rec1) as _c1, Client(server, message_handler=rec2) as _c2:
        # Broadcast to all connected sessions
        await server.broadcast_resource_updated("resource://multi/test")
        await server.broadcast_resource_list_changed()

        # Both recorders should receive both notifications
        await rec1.wait_updated()
        await rec2.wait_updated()
        await rec1.wait_list_changed()
        await rec2.wait_list_changed()

    assert "resource://multi/test" in rec1.updated
    assert "resource://multi/test" in rec2.updated
    assert rec1.list_changed >= 1
    assert rec2.list_changed >= 1
