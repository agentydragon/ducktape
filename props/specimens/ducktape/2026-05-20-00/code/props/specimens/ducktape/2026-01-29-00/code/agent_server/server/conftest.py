"""Fixtures for agent_server/server tests.

Moved from agent_server/conftest.py because they are only consumed by tests in this directory.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import mcp.types
import pytest

from agent_core.events import EventType, ToolCall, ToolCallOutput, UserText
from agent_server.persist.events import EventRecord
from agent_server.server.protocol import FunctionCallOutput
from agent_server.server.state import new_state


@pytest.fixture
def fresh_ui_state():
    """Fresh UI state for reducer tests."""
    return new_state()


@pytest.fixture
def make_call_result() -> Callable[[dict[str, Any] | None, bool], mcp.types.CallToolResult]:
    """Factory for MCP CallToolResult."""

    def _make(structured_content: dict[str, Any] | None = None, is_error: bool = False) -> mcp.types.CallToolResult:
        return mcp.types.CallToolResult(content=[], structuredContent=structured_content or {}, isError=is_error)

    return _make


@pytest.fixture
def make_tool_call_output(
    make_call_result: Callable[[dict[str, Any] | None, bool], mcp.types.CallToolResult],
) -> Callable[[str, dict[str, Any] | None, bool], ToolCallOutput]:
    """Factory for ToolCallOutput events."""

    def _make(call_id: str, structured_content: dict[str, Any] | None = None, is_error: bool = False) -> ToolCallOutput:
        return ToolCallOutput(call_id=call_id, result=make_call_result(structured_content, is_error))

    return _make


@pytest.fixture
def make_function_output(
    make_call_result: Callable[[dict[str, Any] | None, bool], mcp.types.CallToolResult],
) -> Callable[[str, dict[str, Any] | None, bool], FunctionCallOutput]:
    """Factory for protocol FunctionCallOutput (not EventRecord)."""

    def _make(
        call_id: str, structured_content: dict[str, Any] | None = None, is_error: bool = False
    ) -> FunctionCallOutput:
        return FunctionCallOutput(call_id=call_id, result=make_call_result(structured_content, is_error))

    return _make


# --- EventRecord factories for history tests ---


@pytest.fixture
def event_ts() -> datetime:
    """Shared timestamp for EventRecord tests."""
    return datetime.now(UTC)


@pytest.fixture
def make_event_record(event_ts: datetime) -> Callable[[EventType, int | None], EventRecord]:
    """Wrap any EventType in an EventRecord with auto-sequencing."""
    seq_counter = {"count": 0}

    def _wrap(payload: EventType, seq: int | None = None) -> EventRecord:
        if seq is None:
            seq_counter["count"] += 1
            seq = seq_counter["count"]
        return EventRecord(seq=seq, ts=event_ts, payload=payload)

    return _wrap


@pytest.fixture
def make_user_text_event(
    make_event_record: Callable[[EventType, int | None], EventRecord],
) -> Callable[[int, str], EventRecord]:
    """Factory for UserText EventRecord."""

    def _make(seq: int, text: str) -> EventRecord:
        return make_event_record(UserText(text=text), seq)

    return _make


@pytest.fixture
def make_tool_call_event(
    make_event_record: Callable[[EventType, int | None], EventRecord], make_tool_call: Callable[..., ToolCall]
) -> Callable[..., EventRecord]:
    """Factory for ToolCall EventRecord."""
    from mcp_infra.prefix import MCPMountPrefix

    def _make(seq: int, server: MCPMountPrefix, tool: str, args: dict[str, Any] | None = None) -> EventRecord:
        return make_event_record(make_tool_call(server, tool, args=args), seq)

    return _make


@pytest.fixture
def make_function_output_event(
    make_event_record: Callable[[EventType, int | None], EventRecord],
    make_tool_call_output: Callable[[str, dict[str, Any] | None, bool], ToolCallOutput],
) -> Callable[[int, str, dict[str, Any] | None], EventRecord]:
    """Factory for ToolCallOutput EventRecord."""

    def _make(seq: int, call_id: str, structured_content: dict[str, Any] | None = None) -> EventRecord:
        return make_event_record(make_tool_call_output(call_id, structured_content, False), seq)

    return _make
