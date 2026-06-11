"""RecordingHandler and related pytest fixtures for agent tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent_core.events import AssistantText, SystemText, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler, FinishOnTextMessageHandler


class RecordingHandler(BaseHandler):
    """Handler that records all events for test assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[ToolCall | ToolCallOutput | SystemText | UserText | AssistantText] = []

    def on_tool_call_event(self, evt: ToolCall, /) -> None:
        self.records.append(evt)

    def on_tool_result_event(self, evt: ToolCallOutput, /) -> None:
        self.records.append(evt)

    def on_system_text_event(self, evt: SystemText, /) -> None:
        self.records.append(evt)

    def on_user_text_event(self, evt: UserText, /) -> None:
        self.records.append(evt)

    def on_assistant_text_event(self, evt: AssistantText, /) -> None:
        self.records.append(evt)


@pytest.fixture
def recording_handler() -> RecordingHandler:
    """Fresh RecordingHandler for capturing agent events during tests."""
    return RecordingHandler()


@pytest.fixture
def test_handlers(recording_handler: RecordingHandler) -> list:
    """Standard handler list for agent tests.

    Includes:
    - FinishOnTextMessageHandler: Abort loop on text messages (test mocks often return text)
    - RecordingHandler: Capture events for assertions
    """
    return [FinishOnTextMessageHandler(), recording_handler]


@pytest.fixture
def call_id_gen() -> Callable[[], str]:
    """Lightweight call_id generator for tests."""
    counter = {"count": 0}

    def _gen() -> str:
        counter["count"] += 1
        return f"test_call:{counter['count']}"

    return _gen
