"""Codex app-server notifications fold into neutral operations, runner-side.

The exhaustive wire-fidelity coverage is the Console projector's (`codex_app_server/test_projection`);
here the runner-side neutral shape is pinned — a prompt opens the turn, items carry it, and
`turn/completed` closes it — since the runner, not the Console, now draws those brackets.
"""

from __future__ import annotations

from itertools import count
from typing import Any
from uuid import UUID, uuid4

import pytest_bazel

from haku.runner.codex.projection import CodexProjector
from haku.runner.neutral_operations import (
    ItemCompleted,
    ItemOpened,
    ItemSegment,
    MessageCompletion,
    MessageOpen,
    Operation,
    ToolCallCompletion,
    ToolCallOpen,
    ToolOutcome,
    TurnAnswered,
    TurnEnded,
    TurnFailed,
    TurnOpened,
)


def _sequential_ids() -> Any:
    counter = count(1)
    return lambda: UUID(int=next(counter))


def _notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"method": method, "params": params}


def _observe(projector: CodexProjector, frames: list[tuple[int, dict[str, Any]]]) -> list[Operation]:
    operations: list[Operation] = []
    for seq, payload in frames:
        operations.extend(projector.observe(seq, payload).operations)
    return operations


def _kinds(operations: list[Operation]) -> list[str]:
    return [op.kind for op in operations]


def test_a_prompt_opens_the_turn_and_its_completion_closes_it() -> None:
    projector = CodexProjector(mint_id=_sequential_ids())
    prompt_id = uuid4()
    opened = list(projector.admit(prompt_id, after_batch_seq=None, frame_seq=1).operations)
    assert _kinds(opened) == ["prompt.admitted", "turn.opened"]
    turn = next(op for op in opened if isinstance(op, TurnOpened))
    assert turn.turn_id == UUID(int=1)

    ended = _observe(projector, [(9, _notification("turn/completed", {"turn": {"status": "completed"}}))])
    assert _kinds(ended) == ["turn.ended"]
    assert isinstance(ended[0], TurnEnded)
    assert ended[0].turn_id == UUID(int=1)
    assert isinstance(ended[0].end, TurnAnswered)


def test_an_agent_message_streams_open_segment_and_completion_under_the_open_turn() -> None:
    projector = CodexProjector(mint_id=_sequential_ids())
    projector.admit(uuid4(), after_batch_seq=None, frame_seq=1)
    operations = _observe(
        projector,
        [
            (2, _notification("item/started", {"item": {"type": "agentMessage", "id": "m1"}})),
            (3, _notification("item/agentMessage/delta", {"itemId": "m1", "delta": "hel"})),
            (4, _notification("item/agentMessage/delta", {"itemId": "m1", "delta": "lo"})),
            (5, _notification("item/completed", {"item": {"type": "agentMessage", "id": "m1", "text": "hello"}})),
        ],
    )
    assert _kinds(operations) == ["item.opened", "item.segment", "item.segment", "item.completed"]
    opened = operations[0]
    assert isinstance(opened, ItemOpened)
    assert isinstance(opened.item, MessageOpen)
    assert opened.turn_id == UUID(int=1)
    assert opened.backend_item_id == "m1"
    # The completion's text was already delivered as deltas, so it adds no segment of its own.
    assert isinstance(operations[3], ItemCompleted)
    assert isinstance(operations[3].completion, MessageCompletion)
    assert all(isinstance(op, ItemSegment) and op.item_id == opened.item_id for op in operations[1:3])


def test_a_command_execution_opens_and_completes_a_tool_call() -> None:
    projector = CodexProjector(mint_id=_sequential_ids())
    projector.admit(uuid4(), after_batch_seq=None, frame_seq=1)
    operations = _observe(
        projector,
        [
            (
                2,
                _notification(
                    "item/started", {"item": {"type": "commandExecution", "id": "c1", "command": "ls", "cwd": "/w"}}
                ),
            ),
            (
                3,
                _notification(
                    "item/completed",
                    {"item": {"type": "commandExecution", "id": "c1", "status": "completed", "exitCode": 0}},
                ),
            ),
        ],
    )
    assert _kinds(operations) == ["item.opened", "item.completed"]
    opened, completed = operations
    assert isinstance(opened, ItemOpened)
    assert isinstance(opened.item, ToolCallOpen)
    assert opened.item.tool_name == "commandExecution"
    assert opened.item.arguments == {"command": "ls", "cwd": "/w"}
    assert isinstance(completed, ItemCompleted)
    assert isinstance(completed.completion, ToolCallCompletion)
    assert completed.completion.outcome is ToolOutcome.SUCCEEDED
    assert completed.completion.structured == {"status": "completed", "exitCode": 0}


def test_a_failed_turn_carries_the_error_message() -> None:
    projector = CodexProjector(mint_id=_sequential_ids())
    projector.admit(uuid4(), after_batch_seq=None, frame_seq=1)
    (ended,) = _observe(
        projector,
        [(2, _notification("turn/completed", {"turn": {"status": "failed", "error": {"message": "out of tokens"}}}))],
    )
    assert isinstance(ended, TurnEnded)
    assert isinstance(ended.end, TurnFailed)
    assert ended.end.failure == "out of tokens"


def test_an_unmapped_notification_is_counted_not_dropped() -> None:
    projector = CodexProjector(mint_id=_sequential_ids())
    projected = projector.observe(2, _notification("item/started", {"item": {"type": "webSearch", "id": "w1"}}))
    assert projected.operations == ()
    assert projected.unprojected == {"item/started/webSearch": 1}


if __name__ == "__main__":
    pytest_bazel.main()
