"""Assertions over Claude Code's native stream-json output frames."""

from __future__ import annotations

from typing import Any

from x.agentplane.native.claude import wire
from x.agentplane.native.claude.blocks import TextBlock, ToolUseBlock

Frame = dict[str, Any]


def parse(frames: list[Frame]) -> list[wire.ClaudeFrame]:
    return [wire.parse_frame(frame) for frame in frames]


def tool_uses(frames: list[Frame]) -> list[ToolUseBlock]:
    return [
        block
        for frame in parse(frames)
        if isinstance(frame, wire.AssistantFrame)
        for block in frame.message.content
        if isinstance(block, ToolUseBlock)
    ]


def tool_results(frames: list[Frame]) -> list[dict[str, Any] | str]:
    """The harness's `tool_use_result` of each tool round trip: structured on success, a message on
    failure."""
    return [
        frame.tool_use_result
        for frame in parse(frames)
        if isinstance(frame, wire.UserFrame) and frame.tool_use_result is not None
    ]


def assistant_texts(frames: list[Frame]) -> list[str]:
    return [
        block.text
        for frame in parse(frames)
        if isinstance(frame, wire.AssistantFrame)
        for block in frame.message.content
        if isinstance(block, TextBlock)
    ]


def terminals(frames: list[Frame]) -> list[wire.ResultFrame]:
    return [frame for frame in parse(frames) if isinstance(frame, wire.ResultFrame)]


def retry_notices(frames: list[Frame]) -> list[wire.SystemFrame]:
    return [
        frame
        for frame in parse(frames)
        if isinstance(frame, wire.SystemFrame) and "attempt" in (frame.model_extra or {})
    ]


def assert_tool_lifecycles(frames: list[Frame], expected_names: list[str]) -> list[dict[str, Any] | str]:
    assert [block.name for block in tool_uses(frames)] == expected_names
    events = [frame.event for frame in parse(frames) if isinstance(frame, wire.StreamEventFrame)]
    assert sum(isinstance(event, wire.ContentBlockStart) for event in events) >= len(expected_names)
    assert sum(isinstance(event, wire.ContentBlockStop) for event in events) >= len(expected_names)
    results = tool_results(frames)
    assert len(results) >= len(expected_names)
    return results


def assert_success(frames: list[Frame], expected: str) -> wire.ResultFrame:
    result = terminals(frames)[-1]
    assert result.is_error is False
    assert result.stop_reason == "end_turn"
    assert result.terminal_reason == "completed"
    assert result.result == expected
    assert expected in assistant_texts(frames)
    return result


def assert_failure(result: wire.ResultFrame, *, result_fragment: str, terminal_reason: str) -> None:
    assert result.is_error is True
    assert result.terminal_reason == terminal_reason
    assert result_fragment in (result.result or "")
