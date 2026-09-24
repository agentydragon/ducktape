"""The Codex adapter over the typed native facade, and what it records from Codex's answers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest_bazel
from pydantic import BaseModel

from agentplane.native.codex import wire
from agentplane.native.transport import FrameMatcher, NativeReceipt
from agentplane.protocol import event_pb2
from agentplane.runner.codex import CodexAdapter
from agentplane.runner.config import CodexLaunch
from agentplane.runner.observation import Observation
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord

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


def _adapter(recorded: RecordedSession) -> CodexAdapter:
    return CodexAdapter(
        cast(Session, recorded), CodexLaunch(binary=Path("/bin/false"), base_url="http://unused", api_key="unused")
    )


def _frame(frame: BaseModel) -> dict[str, object]:
    return frame.model_dump(mode="json", by_alias=True, exclude_none=True)


def test_codex_adapter_attaches_the_session_to_the_shared_native_facade() -> None:
    recorded = RecordedSession()
    assert cast(object, _adapter(recorded).harness.transport) is recorded


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


if __name__ == "__main__":
    pytest_bazel.main()
