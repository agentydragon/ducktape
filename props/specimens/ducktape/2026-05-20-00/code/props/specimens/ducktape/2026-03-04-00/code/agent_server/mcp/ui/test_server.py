from __future__ import annotations

import pytest
import pytest_bazel

from agent_core.loop_control import Abort, NoAction
from agent_server.mcp.ui.server import EndTurnInput, SendMessageInput, UiServer
from agent_server.server.bus import MimeType, ServerBus, UiEndTurn, UiMessage
from agent_server.server.mode_handler import ServerModeHandler
from mcp_infra.notifications.types import NotificationsBatch


@pytest.fixture
def bus():
    return ServerBus()


async def test_ui_send_message_and_end_turn_bus(bus, make_typed_mcp) -> None:
    server = UiServer(bus)

    async with make_typed_mcp(server) as (client, _sess):
        # Send a markdown message directly via typed client
        msg = SendMessageInput(mime=MimeType.MARKDOWN.value, content="**hello**")
        out: UiMessage = await client.send_message(msg)
        assert out.mime == "text/markdown"
        assert out.content == "**hello**"

        drained = bus.drain_messages()
        assert drained
        assert isinstance(drained[0], UiMessage)
        assert drained[0].content == "**hello**"

        # Request end_turn
        await client.end_turn(EndTurnInput())
        # bus flag is set and an UiEndTurn item was queued
        assert bus.end_turn_requested is True
        assert any(isinstance(x, UiEndTurn) for x in bus.drain_messages())


def test_ui_handler_abort_on_end_turn(bus) -> None:
    def dummy_poll():
        return NotificationsBatch()

    h = ServerModeHandler(bus=bus, poll_notifications=dummy_poll)

    # No end_turn pending -> NoAction
    assert isinstance(h.on_before_sample(), NoAction)

    # Push end_turn -> handler should Abort
    bus.push_end_turn()
    dec2 = h.on_before_sample()
    assert isinstance(dec2, Abort)


if __name__ == "__main__":
    pytest_bazel.main()
