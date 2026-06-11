import asyncio

import pytest_bazel
from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from mcp import types

from agent_server.mcp.chat.server import attach_chat_servers


class _Capture(MessageHandler):
    def __init__(self) -> None:
        self.updated: list[str] = []

    # Override with narrower type than base MessageHandler (which accepts Any)
    async def on_resource_updated(self, message: types.ResourceUpdatedNotification) -> None:
        self.updated.append(str(message.params.uri))


async def test_chat_head_notifications_other_participant(compositor) -> None:
    _store, human, assistant = await attach_chat_servers(compositor)

    # Connect directly to assistant server to capture its notifications
    cap_assist = _Capture()
    async with Client(assistant, message_handler=cap_assist) as assist_sess, Client(human) as human_sess:
        # Human posts; assistant should receive a chat://head update
        out = await human_sess.call_tool(name="post", arguments={"mime": "text/markdown", "content": "hello"})
        assert not out.is_error
        await asyncio.sleep(0.05)
        assert str(assistant.head_resource.uri) in cap_assist.updated, cap_assist.updated

    # Connect directly to human server to capture its notifications
    cap_human = _Capture()
    async with Client(human, message_handler=cap_human) as human_sess, Client(assistant) as assist_sess:
        out2 = await assist_sess.call_tool(name="post", arguments={"mime": "text/markdown", "content": "roger"})
        assert not out2.is_error
        await asyncio.sleep(0.05)
        assert str(human.head_resource.uri) in cap_human.updated, cap_human.updated


if __name__ == "__main__":
    pytest_bazel.main()
