"""Assertions over Codex app-server's native JSON-RPC output frames."""

from __future__ import annotations

from typing import Any

Frame = dict[str, Any]


def items(frames: list[Frame], *, item_type: str | None = None) -> list[Frame]:
    result = []
    for frame in frames:
        if frame.get("method") not in {"item/started", "item/completed"}:
            continue
        item = frame.get("params", {}).get("item")
        if isinstance(item, dict) and (item_type is None or item.get("type") == item_type):
            result.append(item)
    return result


def completed_turns(frames: list[Frame]) -> list[Frame]:
    return [
        frame["params"]["turn"]
        for frame in frames
        if frame.get("method") == "turn/completed" and isinstance(frame.get("params", {}).get("turn"), dict)
    ]


def agent_texts(frames: list[Frame]) -> list[str]:
    return [
        item["text"]
        for item in items(frames, item_type="agentMessage")
        if isinstance(item.get("text"), str) and item["text"]
    ]


def errors(frames: list[Frame]) -> list[Frame]:
    return [frame["params"] for frame in frames if frame.get("method") == "error"]


def assert_success(frames: list[Frame], expected: str) -> list[Frame]:
    turns = completed_turns(frames)
    assert turns
    assert turns[-1]["status"] == "completed"
    assert agent_texts(frames)[-1] == expected
    return turns


def assert_item_lifecycles(frames: list[Frame], item_type: str) -> list[Frame]:
    """Completed items of the type, each of which was also announced as started."""
    started = {
        item["id"]
        for frame in frames
        if frame.get("method") == "item/started"
        for item in [frame.get("params", {}).get("item")]
        if isinstance(item, dict) and item.get("type") == item_type and item.get("id")
    }
    completed = [
        item
        for frame in frames
        if frame.get("method") == "item/completed"
        for item in [frame.get("params", {}).get("item")]
        if isinstance(item, dict) and item.get("type") == item_type
    ]
    assert completed
    assert all(item.get("id") in started for item in completed)
    return completed
