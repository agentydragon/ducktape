"""Assertions over Codex app-server's native JSON-RPC output frames."""

from __future__ import annotations

from typing import Any

from x.agentplane.native.codex import wire

Frame = dict[str, Any]


def parse(frames: list[Frame]) -> list[wire.CodexFrame]:
    return [wire.parse_frame(frame) for frame in frames]


def items(frames: list[Frame]) -> list[wire.Item]:
    """Every item as it was announced started or completed, in order."""
    return [frame.params.item for frame in parse(frames) if isinstance(frame, wire.ItemStarted | wire.ItemCompleted)]


def completed_turns(frames: list[Frame]) -> list[wire.Turn]:
    return [frame.params.turn for frame in parse(frames) if isinstance(frame, wire.TurnCompleted)]


def agent_texts(frames: list[Frame]) -> list[str]:
    return [item.text for item in items(frames) if isinstance(item, wire.AgentMessageItem) and item.text]


def errors(frames: list[Frame]) -> list[wire.ErrorParams]:
    return [frame.params for frame in parse(frames) if isinstance(frame, wire.ErrorNotification)]


def assert_success(frames: list[Frame], expected: str) -> list[wire.Turn]:
    turns = completed_turns(frames)
    assert turns
    assert turns[-1].status is wire.TurnStatus.COMPLETED
    assert agent_texts(frames)[-1] == expected
    return turns


def assert_item_lifecycles[
    T: wire.UserMessageItem | wire.AgentMessageItem | wire.ReasoningItem | wire.CommandExecutionItem
](frames: list[Frame], item_model: type[T]) -> list[T]:
    """Completed items of the model's type, each of which was also announced as started."""
    started = {
        frame.params.item.id
        for frame in parse(frames)
        if isinstance(frame, wire.ItemStarted) and isinstance(frame.params.item, item_model)
    }
    completed = [
        frame.params.item
        for frame in parse(frames)
        if isinstance(frame, wire.ItemCompleted) and isinstance(frame.params.item, item_model)
    ]
    assert completed
    assert all(item.id in started for item in completed)
    return completed
