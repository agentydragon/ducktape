from __future__ import annotations

from typing import Any, cast

import pytest
import pytest_bazel
from fastapi import WebSocket
from pydantic import ValidationError

from haku.console.console_events import ConsoleEventHub, ConsoleHelloEvent, McpOperatorAuthChangedEvent


class RecordingWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.messages: list[dict[str, Any]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


async def test_event_hub_routes_by_operator_subject() -> None:
    hub = ConsoleEventHub("postgresql+psycopg://unused")
    first = RecordingWebSocket()
    second = RecordingWebSocket()
    await hub.connect(cast(WebSocket, first), "operator-a")
    await hub.connect(cast(WebSocket, second), "operator-b")

    event = McpOperatorAuthChangedEvent(server_id="grocy-sf", status="connected")
    await hub._deliver_locally("operator-a", event)

    assert first.accepted is True
    assert second.accepted is True
    assert first.messages == [event.model_dump(mode="json")]
    assert second.messages == []


def test_operator_auth_event_is_pydantic_validated() -> None:
    with pytest.raises(ValidationError):
        McpOperatorAuthChangedEvent.model_validate(
            {"event_type": "mcp_operator_auth_changed", "server_id": "grocy-sf", "status": "unknown"}
        )


def test_console_hello_event_is_a_pydantic_shape() -> None:
    assert ConsoleHelloEvent().model_dump(mode="json") == {"event_type": "hello"}


if __name__ == "__main__":
    pytest_bazel.main()
