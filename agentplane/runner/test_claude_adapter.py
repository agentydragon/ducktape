"""Claude replay translation, using the raw batch shape pinned by the native harness test."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_bazel
from pydantic import BaseModel

from agentplane.native.claude import wire
from agentplane.native.claude.blocks import ToolResultBlock
from agentplane.native.transport import FrameMatcher, NativeReceipt
from agentplane.protocol import event_pb2
from agentplane.runner.claude import ClaudeAdapter
from agentplane.runner.config import ClaudeLaunch
from agentplane.runner.journal import Journal
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord
from agentplane.runner.testing.claude_transcript import (
    Block,
    assistant_message,
    text_block,
    thinking_block,
    tool_use_block,
    user_message,
    write_transcript,
)

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


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
        self.confirmed_sources: list[list[int]] = []
        self.emitted: list[object] = []
        self.noops: list[tuple[str, str]] = []
        self.model_changes: list[tuple[str, str, list[int]]] = []
        self.effort_changes: list[tuple[str, str, list[int]]] = []
        self.failures: list[tuple[str, str, list[int]]] = []
        self.requested = asyncio.Event()
        self.reply: asyncio.Future[NativeReceipt] | None = None

    async def send(self, frame: BaseModel) -> None:
        self.native.append(frame)

    async def request(self, frame: BaseModel, *, matches: FrameMatcher, timeout_s: float = 60) -> NativeReceipt:
        self.native.append(frame)
        self.reply = asyncio.get_running_loop().create_future()
        self.requested.set()
        return await self.reply

    async def confirm_user_message(
        self, *, harness_message_id: str, text: str, origin_command_ids: list[str], turn_id: str, sources: list[int]
    ) -> None:
        self.confirmed.append((harness_message_id, text, origin_command_ids, turn_id))
        self.confirmed_sources.append(sources)

    async def _noop(self, command_id: str, reason: str, *, sources: list[int]) -> None:
        self.noops.append((command_id, reason))

    async def _fail(self, command_id: str, reason: str, *, sources: list[int]) -> None:
        self.failures.append((command_id, reason, sources))

    async def model_changed(self, command_id: str, model: str, *, sources: list[int]) -> None:
        self.model_changes.append((command_id, model, sources))

    async def reasoning_effort_changed(self, command_id: str, effort: str, *, sources: list[int]) -> None:
        self.effort_changes.append((command_id, effort, sources))

    async def emit(self, observation: object, *, sources: list[int]) -> None:
        self.emitted.append(observation)


NATIVE_SESSION = "native-session"


class JournaledSession(RecordedSession):
    """The seam `reconcile` reads: the journal of the turn and the harness's native directory."""

    def __init__(self, journal: Journal, native_directory: Path) -> None:
        super().__init__()
        self.journal = journal
        self.native_directory = native_directory
        self.record.native_session_id = NATIVE_SESSION


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    async with Journal.open(tmp_path / "journal.sqlite", "test-source") as opened:
        yield opened


def _launch() -> ClaudeLaunch:
    return ClaudeLaunch(binary=Path("/bin/false"), base_url="http://unused", auth_token="unused")


async def _write_block(journal: Journal, message_id: str, block: Block) -> None:
    """The assistant frame Claude wrote for one block of a message, as the runner journals it."""
    frame = {
        "type": "assistant",
        "message": {"id": message_id, "content": [block]},
        "session_id": NATIVE_SESSION,
        "uuid": "frame-uuid",
    }
    await journal.append(event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line=json.dumps(frame)))


async def _observe_text(journal: Journal, item_id: str, words: str, *, completed: bool) -> None:
    await journal.append(event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT))
    await journal.append(event_pb2.TextDelta(item_id=item_id, text=words))
    if completed:
        await journal.append(event_pb2.ItemCompleted(item_id=item_id, text=words))


async def _observe_reasoning(journal: Journal, item_id: str, thought: str, *, completed: bool) -> None:
    await journal.append(event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_REASONING))
    await journal.append(event_pb2.TextDelta(item_id=item_id, text=thought))
    if completed:
        await journal.append(event_pb2.ItemCompleted(item_id=item_id, text=thought))


def _dispositions(reconciled: event_pb2.ConversationReconciled) -> list[tuple[str, event_pb2.RecoveryDisposition]]:
    return [(item.item_id, item.disposition) for item in reconciled.items]


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


def _control_response(request_id: str, *, error: str | None = None) -> dict[str, object]:
    return wire.ControlResponseFrame(
        type="control_response",
        response=wire.ControlResponseBody(
            subtype="error" if error is not None else "success", request_id=request_id, error=error
        ),
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
            content=[ToolResultBlock(type="tool_result", tool_use_id="tool-1", content="tool output", is_error=False)],
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


async def test_a_model_switch_is_recorded_when_its_answer_is_translated() -> None:
    """The answer's effect is recorded in frame order, before `change_model` gets the answer back."""
    recorded = RecordedSession()
    adapter = ClaudeAdapter(
        cast(Session, recorded), ClaudeLaunch(binary=Path("/bin/false"), base_url="http://unused", auth_token="unused")
    )
    switch = asyncio.create_task(adapter.change_model("switch-1", "test-switched-model"))
    await recorded.requested.wait()
    answer = _control_response(cast(wire.SetModelRequest, recorded.native[-1]).request_id)
    await adapter.on_frame(answer, 7)
    assert recorded.model_changes == [("switch-1", "test-switched-model", [7])]
    assert recorded.reply is not None
    recorded.reply.set_result(NativeReceipt(answer, 7))
    await switch


async def test_a_refused_model_switch_fails_its_command() -> None:
    recorded = RecordedSession()
    adapter = ClaudeAdapter(
        cast(Session, recorded), ClaudeLaunch(binary=Path("/bin/false"), base_url="http://unused", auth_token="unused")
    )
    switch = asyncio.create_task(adapter.change_model("switch-1", "test-unknown-model"))
    await recorded.requested.wait()
    answer = _control_response(cast(wire.SetModelRequest, recorded.native[-1]).request_id, error="unknown model")
    await adapter.on_frame(answer, 7)
    assert recorded.reply is not None
    recorded.reply.set_result(NativeReceipt(answer, 7))
    await switch
    assert (recorded.model_changes, recorded.failures) == (
        [],
        [("switch-1", "Claude Code refused model switch: unknown model", [7])],
    )


async def test_effort_requires_matching_successful_native_control_response() -> None:
    recorded = RecordedSession()
    adapter = ClaudeAdapter(
        cast(Session, recorded), ClaudeLaunch(binary=Path("/bin/false"), base_url="http://unused", auth_token="unused")
    )
    change = asyncio.create_task(adapter.change_reasoning_effort("effort-1", "high"))
    await recorded.requested.wait()
    request = cast(wire.ApplyFlagSettingsRequest, recorded.native[-1])
    assert request.request.settings == {"effortLevel": "high"}
    assert recorded.effort_changes == []
    answer = _control_response(request.request_id)
    await adapter.on_frame(answer, 7)
    assert recorded.effort_changes == [("effort-1", "high", [7])]
    assert recorded.reply is not None
    recorded.reply.set_result(NativeReceipt(answer, 7))
    await change

    recorded.requested.clear()
    change = asyncio.create_task(adapter.change_reasoning_effort("effort-2", "low"))
    await recorded.requested.wait()
    answer = _control_response(cast(wire.ApplyFlagSettingsRequest, recorded.native[-1]).request_id, error="denied")
    await adapter.on_frame(answer, 9)
    assert recorded.reply is not None
    recorded.reply.set_result(NativeReceipt(answer, 9))
    await change
    assert recorded.effort_changes == [("effort-1", "high", [7])]
    assert recorded.failures[-1] == ("effort-2", "Claude Code refused reasoning effort switch: denied", [9])


async def test_a_resumed_conversation_reports_reasoning_by_what_the_transcript_kept(
    journal: Journal, tmp_path: Path
) -> None:
    write_transcript(
        tmp_path / "claude",
        NATIVE_SESSION,
        user_message("input"),
        assistant_message("kept", thinking_block("KEPT_THOUGHT")),
        assistant_message("kept", text_block("ANSWER")),
        assistant_message("finished-late", thinking_block("FINISHED_THOUGHT")),
        assistant_message("finished-late", text_block("ANSWER")),
        assistant_message("tool", thinking_block("THOUGHT_BEFORE_UNANSWERED_TOOL")),
        assistant_message("tool", tool_use_block("call")),
    )
    await journal.append(event_pb2.TurnStarted(turn_id="turn"))
    await _observe_reasoning(journal, "kept#0", "KEPT_THOUGHT", completed=True)
    # The journal holds only the first fragment of a block the harness went on to finish.
    await _observe_reasoning(journal, "finished-late#0", "FINISHED", completed=False)
    await _observe_reasoning(journal, "tool#0", "THOUGHT_BEFORE_UNANSWERED_TOOL", completed=True)
    await _observe_reasoning(journal, "cut-off#0", "CUT_OFF_THOUGHT", completed=False)

    reconciled = await ClaudeAdapter(cast(Session, JournaledSession(journal, tmp_path)), _launch()).reconcile(
        "turn", resumed=True
    )

    assert _dispositions(reconciled) == [
        ("kept#0", event_pb2.RECOVERY_DISPOSITION_RETAINED),
        ("finished-late#0", event_pb2.RECOVERY_DISPOSITION_REVISED),
        ("tool#0", event_pb2.RECOVERY_DISPOSITION_ABSENT),
        ("cut-off#0", event_pb2.RECOVERY_DISPOSITION_ABSENT),
    ]
    assert reconciled.items[1].replacement.text == "FINISHED_THOUGHT"


async def test_an_interrupted_conversation_keeps_thinking_only_beside_a_block_that_survived(
    journal: Journal, tmp_path: Path
) -> None:
    """The live process drops a thinking block unless its message has another block it wrote."""
    await journal.append(event_pb2.TurnStarted(turn_id="turn"))
    await _observe_reasoning(journal, "cut-off#0", "CUT_OFF_THOUGHT", completed=False)

    await _write_block(journal, "with-text", thinking_block("THOUGHT"))
    await _observe_reasoning(journal, "with-text#0", "THOUGHT", completed=True)
    await _write_block(journal, "with-text", text_block("PARTIAL_ANSWER"))
    await _observe_text(journal, "with-text#1", "PARTIAL_ANSWER", completed=True)

    await _write_block(journal, "with-call", thinking_block("THOUGHT"))
    await _observe_reasoning(journal, "with-call#0", "THOUGHT", completed=True)
    await _write_block(journal, "with-call", tool_use_block("call"))
    await journal.append(event_pb2.ItemStarted(item_id="call", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="Bash"))

    await _write_block(journal, "alone", thinking_block("THOUGHT"))
    await _observe_reasoning(journal, "alone#0", "THOUGHT", completed=True)

    # The interrupt came before Claude wrote the text that was streaming, or the call it was reading.
    await _write_block(journal, "text-not-written", thinking_block("THOUGHT"))
    await _observe_reasoning(journal, "text-not-written#0", "THOUGHT", completed=True)
    await _observe_text(journal, "text-not-written#1", "", completed=False)
    await _write_block(journal, "call-not-written", thinking_block("THOUGHT"))
    await _observe_reasoning(journal, "call-not-written#0", "THOUGHT", completed=True)
    await journal.append(
        event_pb2.ItemStarted(item_id="unwritten-call", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="Bash")
    )

    reconciled = await ClaudeAdapter(cast(Session, JournaledSession(journal, tmp_path)), _launch()).reconcile(
        "turn", resumed=False
    )

    assert dict(_dispositions(reconciled)) == {
        "cut-off#0": event_pb2.RECOVERY_DISPOSITION_ABSENT,
        "with-text#0": event_pb2.RECOVERY_DISPOSITION_RETAINED,
        "with-text#1": event_pb2.RECOVERY_DISPOSITION_RETAINED,
        "with-call#0": event_pb2.RECOVERY_DISPOSITION_RETAINED,
        "call": event_pb2.RECOVERY_DISPOSITION_UNKNOWN,
        "alone#0": event_pb2.RECOVERY_DISPOSITION_ABSENT,
        "text-not-written#0": event_pb2.RECOVERY_DISPOSITION_ABSENT,
        "text-not-written#1": event_pb2.RECOVERY_DISPOSITION_ABSENT,
        "call-not-written#0": event_pb2.RECOVERY_DISPOSITION_ABSENT,
        "unwritten-call": event_pb2.RECOVERY_DISPOSITION_UNKNOWN,
    }


async def test_an_interrupted_conversation_is_unknown_for_thinking_a_line_we_could_not_parse_may_have_answered(
    journal: Journal, tmp_path: Path
) -> None:
    await journal.append(event_pb2.TurnStarted(turn_id="turn"))
    await _write_block(journal, "alone", thinking_block("THOUGHT"))
    await _observe_reasoning(journal, "alone#0", "THOUGHT", completed=True)
    await _write_block(journal, "answered", thinking_block("THOUGHT"))
    await _observe_reasoning(journal, "answered#0", "THOUGHT", completed=True)
    await _write_block(journal, "answered", text_block("ANSWER"))
    await _observe_text(journal, "answered#1", "ANSWER", completed=True)
    await _observe_reasoning(journal, "cut-off#0", "CUT_OFF_THOUGHT", completed=False)
    await journal.append(event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"type": "assistant"'))

    reconciled = await ClaudeAdapter(cast(Session, JournaledSession(journal, tmp_path)), _launch()).reconcile(
        "turn", resumed=False
    )

    # Only thinking that would be reported absent for want of a sibling we may have missed is unknown.
    assert dict(_dispositions(reconciled)) == {
        "alone#0": event_pb2.RECOVERY_DISPOSITION_UNKNOWN,
        "answered#0": event_pb2.RECOVERY_DISPOSITION_RETAINED,
        "answered#1": event_pb2.RECOVERY_DISPOSITION_RETAINED,
        "cut-off#0": event_pb2.RECOVERY_DISPOSITION_ABSENT,
    }


@pytest.mark.parametrize("compacted", [False, True], ids=["no-transcript", "compacted"])
async def test_a_resumed_conversation_is_unknown_where_the_transcript_cannot_say(
    journal: Journal, tmp_path: Path, compacted: bool
) -> None:
    if compacted:
        transcript = write_transcript(
            tmp_path / "claude", NATIVE_SESSION, assistant_message("msg", thinking_block("THOUGHT"))
        )
        with transcript.open("a") as output:
            output.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
    await journal.append(event_pb2.TurnStarted(turn_id="turn"))
    await _observe_reasoning(journal, "msg#0", "THOUGHT", completed=True)

    reconciled = await ClaudeAdapter(cast(Session, JournaledSession(journal, tmp_path)), _launch()).reconcile(
        "turn", resumed=True
    )

    assert _dispositions(reconciled) == [("msg#0", event_pb2.RECOVERY_DISPOSITION_UNKNOWN)]
    assert reconciled.items[0].reason


if __name__ == "__main__":
    pytest_bazel.main()
