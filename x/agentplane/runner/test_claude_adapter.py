"""Claude replay translation, using the raw batch shape pinned by the native harness test."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest_bazel
from pydantic import BaseModel

from x.agentplane.native.claude import wire
from x.agentplane.runner.claude import ClaudeAdapter
from x.agentplane.runner.config import ClaudeLaunch
from x.agentplane.runner.session import Session
from x.agentplane.runner.store import SessionRecord


class RecordedSession:
    """The narrow runner seam ClaudeAdapter owns; native behavior itself is tested separately."""

    def __init__(self) -> None:
        self.record = SessionRecord(
            harness="HARNESS_CLAUDE",
            cwd="/workspace",
            model="agentplane-test/claude-haiku-4-5-20251001",
            reasoning_effort="low",
        )
        self.active_turn_id = "turn-1"
        self.native: list[BaseModel] = []
        self.confirmed: list[tuple[str, str, list[str], str]] = []
        self.confirmed_sources: list[list[int] | None] = []
        self.emitted: list[object] = []
        self.noops: list[tuple[str, str]] = []

    async def send(self, frame: BaseModel) -> None:
        self.native.append(frame)

    async def confirm_user_message(
        self,
        *,
        harness_message_id: str,
        text: str,
        origin_command_ids: list[str],
        turn_id: str,
        sources: list[int] | None = None,
    ) -> None:
        self.confirmed.append((harness_message_id, text, origin_command_ids, turn_id))
        self.confirmed_sources.append(sources)

    async def _noop(self, command_id: str, reason: str) -> None:
        self.noops.append((command_id, reason))

    async def emit(self, observation: object, *, sources: list[int] | None = None) -> None:
        self.emitted.append(observation)


def _lifecycle(command_uuid: str, state: wire.CommandState) -> dict[str, object]:
    return wire.CommandLifecycleFrame(
        type="command_lifecycle",
        command_uuid=command_uuid,
        state=state,
        uuid=f"event-{command_uuid}",
        session_id="native-session",
    ).model_dump(mode="json", by_alias=True)


def _replay(uuid: str, text: str) -> dict[str, object]:
    return wire.UserFrame(
        type="user", message=wire.UserMessage(role="user", content=text), uuid=uuid, isReplay=True
    ).model_dump(mode="json", by_alias=True)


def _message_start(user_message_uuid: str) -> dict[str, object]:
    return wire.StreamEventFrame(
        type="stream_event",
        event=wire.MessageStart(type="message_start", message=wire.StreamedMessage(id="message-1")),
        session_id="native-session",
        uuid="native-message-start",
        user_message_uuid=user_message_uuid,
    ).model_dump(mode="json", by_alias=True)


async def test_one_claude_input_is_confirmed_by_its_model_request_correlation() -> None:
    recorded = RecordedSession()
    adapter = ClaudeAdapter(
        cast(Session, recorded), ClaudeLaunch(binary=Path("/bin/false"), base_url="http://unused", auth_token="unused")
    )
    assert cast(object, adapter.harness.transport) is recorded
    await adapter.submit("input-1", "ONE_INPUT")
    uuid = cast(wire.UserInput, recorded.native[0]).uuid
    await adapter.on_frame(_lifecycle(uuid, wire.CommandState.STARTED), 1)
    await adapter.on_frame(_message_start(uuid), 2)

    assert recorded.confirmed == [(uuid, "ONE_INPUT", ["input-1"], "turn-1")]
    assert recorded.confirmed_sources == [[1, 2]]


async def test_coalesced_replay_confirms_only_the_representative_native_message() -> None:
    recorded = RecordedSession()
    adapter = ClaudeAdapter(
        cast(Session, recorded), ClaudeLaunch(binary=Path("/bin/false"), base_url="http://unused", auth_token="unused")
    )
    first_text = "Reply only after seeing COALESCED_FIRST."
    second_text = "Reply only after seeing COALESCED_SECOND."
    await adapter.submit("input-1", first_text)
    await adapter.submit("input-2", second_text)
    first_uuid = cast(wire.UserInput, recorded.native[0]).uuid
    second_uuid = cast(wire.UserInput, recorded.native[1]).uuid

    # Queue admission does not claim the model received either message.
    await adapter.on_frame(_lifecycle(first_uuid, wire.CommandState.QUEUED), 1)
    await adapter.on_frame(_lifecycle(second_uuid, wire.CommandState.QUEUED), 2)
    assert recorded.confirmed == []

    # This is the actual Claude replay ordering pinned by
    # harness_tests/claude:test_active_turn: a follower echo precedes the batch's started frames.
    await adapter.on_frame(_replay(first_uuid, first_text), 3)
    await adapter.on_frame(_lifecycle(first_uuid, wire.CommandState.STARTED), 4)
    await adapter.on_frame(_lifecycle(second_uuid, wire.CommandState.STARTED), 5)
    assert adapter._started_inputs == [(first_uuid, 4), (second_uuid, 5)]
    representative = _replay(second_uuid, f"{first_text}\n{second_text}")
    parsed = wire.parse_frame(representative)
    assert isinstance(parsed, wire.UserFrame)
    assert parsed.is_replay
    await adapter.on_frame(representative, 6)

    assert recorded.confirmed == [(second_uuid, f"{first_text}\n{second_text}", ["input-1", "input-2"], "turn-1")]
    assert recorded.confirmed_sources == [[4, 5, 6]]


async def test_inputs_started_after_a_tool_result_are_confirmed_as_one_cohort() -> None:
    recorded = RecordedSession()
    adapter = ClaudeAdapter(
        cast(Session, recorded), ClaudeLaunch(binary=Path("/bin/false"), base_url="http://unused", auth_token="unused")
    )
    first_text = "FIRST_ACTIVE_INPUT"
    second_text = "SECOND_ACTIVE_INPUT"
    await adapter.submit("input-1", first_text)
    await adapter.submit("input-2", second_text)
    first_uuid = cast(wire.UserInput, recorded.native[0]).uuid
    second_uuid = cast(wire.UserInput, recorded.native[1]).uuid
    tool_result = wire.UserFrame(
        type="user",
        message=wire.UserMessage(
            role="user",
            content=[{"type": "tool_result", "tool_use_id": "tool-1", "content": "tool output", "is_error": False}],
        ),
        uuid="native-tool-result",
    ).model_dump(mode="json", by_alias=True)
    await adapter.on_frame(tool_result, 10)
    # Claude writes this synthetic batch follower before its active-tool started cohort.
    await adapter.on_frame(_replay(first_uuid, first_text), 11)
    await adapter.on_frame(_lifecycle(first_uuid, wire.CommandState.STARTED), 12)
    await adapter.on_frame(_lifecycle(second_uuid, wire.CommandState.STARTED), 13)
    # The next non-started frame closes Claude's lifecycle cohort for that continuation request.
    await adapter.on_frame(_lifecycle("unrelated", wire.CommandState.QUEUED), 14)

    assert recorded.confirmed == [
        ("native-tool-result", f"{first_text}\n{second_text}", ["input-1", "input-2"], "turn-1")
    ]
    assert recorded.confirmed_sources == [[10, 12, 13]]


async def test_cancelled_claude_queue_input_has_a_terminal_noop() -> None:
    recorded = RecordedSession()
    adapter = ClaudeAdapter(
        cast(Session, recorded), ClaudeLaunch(binary=Path("/bin/false"), base_url="http://unused", auth_token="unused")
    )
    await adapter.submit("input-cancelled", "This input is never taken.")
    uuid = cast(wire.UserInput, recorded.native[0]).uuid
    await adapter.on_frame(_lifecycle(uuid, wire.CommandState.CANCELLED), 1)
    assert recorded.noops == [("input-cancelled", "Claude cancelled the queued user input before taking it")]


if __name__ == "__main__":
    pytest_bazel.main()
