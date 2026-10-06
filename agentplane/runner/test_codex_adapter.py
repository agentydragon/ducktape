"""The Codex adapter over the typed native facade, and what it records from Codex's answers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest_bazel
from pydantic import BaseModel

from agentplane.native.codex import wire
from agentplane.native.transport import FrameMatcher, NativeReceipt
from agentplane.protocol import event_pb2
from agentplane.runner.codex import CodexAdapter
from agentplane.runner.config import CodexLaunch
from agentplane.runner.journal import Journal
from agentplane.runner.observation import Observation
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord
from agentplane.runner.testing import codex_rollout

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class RecordedSession:
    """The runner seam CodexAdapter uses, recording what it is asked to record, in order."""

    def __init__(self) -> None:
        self.record = SessionRecord(harness="HARNESS_CODEX", cwd="/workspace", model="gpt-5.4", reasoning_effort="low")
        self.active_turn_id = ""
        self.native: list[BaseModel] = []
        self.requested = asyncio.Event()
        self.reply: asyncio.Future[NativeReceipt] | None = None
        self.recorded: list[tuple[object, ...]] = []

    async def request(self, frame: BaseModel, *, matches: FrameMatcher, timeout_s: float = 60) -> NativeReceipt:
        self.native.append(frame)
        self.reply = asyncio.get_running_loop().create_future()
        self.requested.set()
        return await self.reply

    async def emit(self, observation: Observation, *, sources: Sequence[int]) -> None:
        self.recorded.append((observation, list(sources)))
        if isinstance(observation, event_pb2.TurnStarted):
            self.active_turn_id = observation.turn_id

    async def model_changed(self, command_id: str, model: str, *, sources: Sequence[int]) -> None:
        self.record.model = model
        self.recorded.append(("model_changed", command_id, model, list(sources)))

    async def reasoning_effort_changed(self, command_id: str, effort: str, *, sources: Sequence[int]) -> None:
        self.record.reasoning_effort = effort
        self.recorded.append(("effort_changed", command_id, effort, list(sources)))

    async def _noop(self, command_id: str, reason: str, *, sources: Sequence[int]) -> None:
        self.recorded.append(("noop", command_id, reason, list(sources)))

    async def _fail(self, command_id: str, reason: str, *, sources: Sequence[int]) -> None:
        self.recorded.append(("failed", command_id, reason, list(sources)))

    async def confirm_user_message(
        self,
        *,
        harness_message_id: str,
        text: str,
        origin_command_ids: Sequence[str],
        turn_id: str,
        sources: Sequence[int],
    ) -> None:
        self.recorded.append(("confirmed", harness_message_id, text, list(origin_command_ids), turn_id, list(sources)))


class JournaledSession(RecordedSession):
    """The seam `reconcile` reads: the turn's journal and the thread's saved Codex history."""

    def __init__(self, journal: Journal, native_directory: Path) -> None:
        super().__init__()
        self.journal = journal
        self.native_directory = native_directory
        self.record.native_session_id = codex_rollout.THREAD_ID


def _adapter(recorded: RecordedSession) -> CodexAdapter:
    return CodexAdapter(
        cast(Session, recorded), CodexLaunch(binary=Path("/bin/false"), base_url="http://unused", api_key="unused")
    )


def _frame(frame: BaseModel) -> dict[str, object]:
    return frame.model_dump(mode="json", by_alias=True, exclude_none=True)


async def test_a_turn_start_answer_is_recorded_before_the_frames_after_it_are_translated() -> None:
    """The answer and `turn/started` can share one stdout read; the turn starts with the model the
    answer selects, and the command's effects are recorded before `submit` gets the answer back."""
    recorded = RecordedSession()
    adapter = _adapter(recorded)
    await adapter.change_model("switch-1", "test-switched-model")
    submit = asyncio.create_task(adapter.submit("input-1", "Say PING"))
    await recorded.requested.wait()
    request = cast(wire.TurnStartRequest, recorded.native[-1])
    assert request.params.model == "test-switched-model"

    turn = wire.Turn(id="test-turn", status=wire.TurnStatus.IN_PROGRESS)
    answer = _frame(wire.Response(id=request.id, result=_frame(wire.TurnResult(turn=turn))))
    await adapter.on_frame(answer, 2)
    await adapter.on_frame(
        _frame(wire.TurnStarted(method="turn/started", params=wire.TurnParams(thread_id="test-thread", turn=turn))), 3
    )
    assert recorded.recorded == [
        ("model_changed", "switch-1", "test-switched-model", [2]),
        (event_pb2.TurnStarted(turn_id="test-turn", model="test-switched-model"), [2]),
        ("confirmed", "test-turn", "Say PING", ["input-1"], "test-turn", [2]),
    ]
    assert recorded.reply is not None
    recorded.reply.set_result(NativeReceipt(answer, 2))
    await submit


async def test_effort_waits_for_matching_turn_start_answer() -> None:
    recorded = RecordedSession()
    adapter = _adapter(recorded)
    await adapter.change_reasoning_effort("effort-1", "high")
    assert recorded.recorded == []
    submit = asyncio.create_task(adapter.submit("input-1", "Say PING"))
    await recorded.requested.wait()
    request = cast(wire.TurnStartRequest, recorded.native[-1])
    assert request.params.effort == "high"
    assert recorded.recorded == []
    answer = _frame(
        wire.Response(
            id=request.id, result=_frame(wire.TurnResult(turn=wire.Turn(id="t", status=wire.TurnStatus.IN_PROGRESS)))
        )
    )
    await adapter.on_frame(answer, 8)
    assert recorded.recorded[0] == ("effort_changed", "effort-1", "high", [8])
    assert recorded.reply is not None
    recorded.reply.set_result(NativeReceipt(answer, 8))
    await submit


TURN_ID = "test-turn"
PATCH_ID = "call_test_patch"
# The items the app-server announced for the turn `codex_rollout.ROLLOUT` saved, plus a `fileChange`
# call, which the adapter cannot match to a saved function call.
OBSERVED_TURN: list[Observation] = [
    event_pb2.TurnStarted(turn_id=TURN_ID),
    event_pb2.ItemStarted(item_id=codex_rollout.FIRST_REASONING_ID, kind=event_pb2.ITEM_KIND_REASONING),
    event_pb2.ItemCompleted(item_id=codex_rollout.FIRST_REASONING_ID, text="first thought"),
    event_pb2.ItemStarted(
        item_id=codex_rollout.CALL_ID, kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="commandExecution"
    ),
    event_pb2.ToolArguments(item_id=codex_rollout.CALL_ID, arguments_json='{"command": "printf CAP"}'),
    event_pb2.ItemCompleted(item_id=codex_rollout.CALL_ID, tool=event_pb2.ToolResult(output="CAP\n", succeeded=True)),
    event_pb2.ItemStarted(item_id=PATCH_ID, kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="fileChange"),
    event_pb2.ToolArguments(item_id=PATCH_ID, arguments_json="{}"),
    event_pb2.ItemCompleted(item_id=PATCH_ID, tool=event_pb2.ToolResult(output="{}", succeeded=True)),
    event_pb2.ItemStarted(item_id=codex_rollout.SECOND_REASONING_ID, kind=event_pb2.ITEM_KIND_REASONING),
    event_pb2.ItemCompleted(item_id=codex_rollout.SECOND_REASONING_ID, text="second thought"),
    event_pb2.ItemStarted(item_id=codex_rollout.MESSAGE_ID, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT),
    event_pb2.ItemCompleted(item_id=codex_rollout.MESSAGE_ID, text="CAP_DONE"),
    event_pb2.TurnCompleted(turn_id=TURN_ID, status=event_pb2.TURN_STATUS_COMPLETED),
]


async def _reconcile(
    tmp_path: Path, rollout: list[dict[str, Any]], observed: list[Observation]
) -> dict[str, event_pb2.ItemRecovery]:
    codex_rollout.write_rollout(tmp_path / "codex", rollout)
    async with Journal.open(tmp_path / "journal.sqlite", "test-source") as journal:
        for observation in observed:
            await journal.append(observation, sources=[])
        report = await _adapter(JournaledSession(journal, tmp_path)).reconcile(TURN_ID, resumed=True)
    return {item.item_id: item for item in report.items}


async def test_reconcile_retains_reasoning_the_saved_history_holds(tmp_path: Path) -> None:
    decisions = await _reconcile(tmp_path, codex_rollout.ROLLOUT, OBSERVED_TURN)
    assert {item_id: item.disposition for item_id, item in decisions.items()} == {
        codex_rollout.FIRST_REASONING_ID: event_pb2.RECOVERY_DISPOSITION_RETAINED,
        codex_rollout.CALL_ID: event_pb2.RECOVERY_DISPOSITION_RETAINED,
        PATCH_ID: event_pb2.RECOVERY_DISPOSITION_UNKNOWN,
        codex_rollout.SECOND_REASONING_ID: event_pb2.RECOVERY_DISPOSITION_RETAINED,
        codex_rollout.MESSAGE_ID: event_pb2.RECOVERY_DISPOSITION_RETAINED,
    }


async def test_reconcile_reports_reasoning_absent_when_the_saved_history_lacks_its_record(tmp_path: Path) -> None:
    unsaved = codex_rollout.SECOND_REASONING_ID
    rollout = [record for record in codex_rollout.ROLLOUT if record["payload"].get("id") != unsaved]
    decisions = await _reconcile(tmp_path, rollout, OBSERVED_TURN)
    assert decisions[codex_rollout.FIRST_REASONING_ID].disposition == event_pb2.RECOVERY_DISPOSITION_RETAINED
    assert decisions[unsaved].disposition == event_pb2.RECOVERY_DISPOSITION_ABSENT


async def test_reconcile_reports_reasoning_cut_off_before_completion_absent(tmp_path: Path) -> None:
    cut_off = "rs_test_cut_off"
    decisions = await _reconcile(
        tmp_path,
        codex_rollout.ROLLOUT,
        [
            event_pb2.TurnStarted(turn_id=TURN_ID),
            event_pb2.ItemStarted(item_id=cut_off, kind=event_pb2.ITEM_KIND_REASONING),
            event_pb2.TextDelta(item_id=cut_off, text="first"),
            event_pb2.TurnCompleted(turn_id=TURN_ID, status=event_pb2.TURN_STATUS_PROCESS_LOST),
        ],
    )
    assert {item_id: item.disposition for item_id, item in decisions.items()} == {
        cut_off: event_pb2.RECOVERY_DISPOSITION_ABSENT
    }


async def test_reconcile_unknown_dispositions_name_their_own_cause(tmp_path: Path) -> None:
    """The history was read here and could not be read there; one reason must not stand for both."""
    read = await _reconcile(tmp_path / "read", codex_rollout.ROLLOUT, OBSERVED_TURN)
    compacted = await _reconcile(tmp_path / "compacted", [*codex_rollout.ROLLOUT, {"type": "compacted"}], OBSERVED_TURN)
    assert {item.disposition for item in compacted.values()} == {event_pb2.RECOVERY_DISPOSITION_UNKNOWN}
    assert read[PATCH_ID].disposition == event_pb2.RECOVERY_DISPOSITION_UNKNOWN
    assert read[PATCH_ID].reason != compacted[PATCH_ID].reason


if __name__ == "__main__":
    pytest_bazel.main()
