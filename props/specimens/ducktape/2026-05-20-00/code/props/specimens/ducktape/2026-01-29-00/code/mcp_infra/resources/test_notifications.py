import asyncio

import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from mcp import types


class _DummyBackend:
    async def list_resources(self, _only=None):
        return []

    async def read_resource(self, server: str, uri: str):
        # Return empty typed ReadResourceResult for compatibility
        return types.ReadResourceResult(contents=[])


class _NotifyCatcher(MessageHandler):
    def __init__(self):
        self.events: list[str] = []

    async def on_resource_list_changed(self, message: types.ResourceListChangedNotification):
        self.events.append("list_changed")


@pytest.mark.asyncio
async def test_resources_list_changed_notification(compositor, resources_server):
    catcher = _NotifyCatcher()
    async with Client(resources_server, message_handler=catcher) as client:
        # Trigger session capture by invoking list
        await client.list_resources()
        # Broadcast list changed from server and ensure the client receives it
        await resources_server.broadcast_resource_list_changed()
        # Allow the event loop to deliver the notification
        for _ in range(10):
            if catcher.events:
                break
            await asyncio.sleep(0.01)
        assert "list_changed" in catcher.events


if __name__ == "__main__":
    pytest_bazel.main()
