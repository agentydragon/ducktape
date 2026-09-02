"""Assertions over Claude Code's native stream-json output frames."""

from __future__ import annotations

from typing import Any

Frame = dict[str, Any]


def tool_uses(frames: list[Frame]) -> list[Frame]:
    return [
        item
        for frame in frames
        if frame.get("type") == "assistant"
        for item in frame.get("message", {}).get("content", [])
        if item.get("type") == "tool_use"
    ]


def tool_results(frames: list[Frame]) -> list[Any]:
    return [frame["tool_use_result"] for frame in frames if frame.get("type") == "user" and "tool_use_result" in frame]


def assistant_texts(frames: list[Frame]) -> list[str]:
    return [
        item["text"]
        for frame in frames
        if frame.get("type") == "assistant"
        for item in frame.get("message", {}).get("content", [])
        if item.get("type") == "text" and isinstance(item.get("text"), str)
    ]


def terminals(frames: list[Frame]) -> list[Frame]:
    return [frame for frame in frames if frame.get("type") == "result"]


def retry_notices(frames: list[Frame]) -> list[Frame]:
    return [frame for frame in frames if frame.get("type") == "system" and "attempt" in frame]


def assert_tool_lifecycles(frames: list[Frame], expected_names: list[str]) -> list[Any]:
    uses = tool_uses(frames)
    assert [item["name"] for item in uses] == expected_names
    stream_events = [
        frame["event"]
        for frame in frames
        if frame.get("type") == "stream_event" and isinstance(frame.get("event"), dict)
    ]
    assert sum(event.get("type") == "content_block_start" for event in stream_events) >= len(expected_names)
    assert sum(event.get("type") == "content_block_stop" for event in stream_events) >= len(expected_names)
    results = tool_results(frames)
    assert len(results) >= len(expected_names)
    return results


def assert_success(frames: list[Frame], expected: str) -> Frame:
    result = terminals(frames)[-1]
    assert result["is_error"] is False
    assert result["stop_reason"] == "end_turn"
    assert result["terminal_reason"] == "completed"
    assert result["result"] == expected
    assert expected in assistant_texts(frames)
    return result


def assert_failure(frames: list[Frame], *, result_fragment: str, terminal_reason: str) -> Frame:
    result = terminals(frames)[-1]
    assert result["is_error"] is True
    assert result["terminal_reason"] == terminal_reason
    assert result_fragment in result.get("result", "")
    return result
