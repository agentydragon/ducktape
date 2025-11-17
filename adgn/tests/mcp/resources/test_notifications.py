import asyncio

from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.server import FastMCP
from mcp import types

from adgn.mcp.resources.server import make_resources_server


class _DummyBackend:
    async def list_resources(self, only=None):
        return []

    async def read_resource(self, server: str, uri: str):
        # Return empty typed ReadResourceResult for compatibility
        return types.ReadResourceResult(contents=[])


class _NotifyCatcher(MessageHandler):
    def __init__(self):
        self.events: list[str] = []

    async def on_resource_list_changed(self, message: types.ResourceListChangedNotification):
        self.events.append("list_changed")


async def test_resources_list_changed_notification():
    # Use a minimal FastMCP as a placeholder gateway client
    gw_server = FastMCP("gw")
    async with Client(gw_server) as gw:
        from adgn.mcp.compositor.server import Compositor

        comp = Compositor("comp")
        server = make_resources_server(name="resources", gateway_client=gw, compositor=comp)
        catcher = _NotifyCatcher()
        async with Client(server, message_handler=catcher) as client:
            # Trigger session capture by invoking list
            await client.list_resources()
            # Broadcast list changed from server and ensure the client receives it
            await server.broadcast_resource_list_changed()  # type: ignore[attr-defined]
            # Allow the event loop to deliver the notification
            for _ in range(10):
                if catcher.events:
                    break
                await asyncio.sleep(0.01)
            assert "list_changed" in catcher.events
