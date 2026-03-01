"""Tests for approval_gate discriminated union models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_bazel
from mcp.types import CallToolResult, TextContent
from pydantic import TypeAdapter, ValidationError

from approval_gate.models import (
    ActionKey,
    ActionReceivedDetail,
    ActionState,
    ActionStatus,
    ApprovedDetail,
    DeniedDetail,
    DoneState,
    ExecutingState,
    ExecutionFinishedDetail,
    ExecutionStartedDetail,
    LogEntry,
    LogEventDetail,
    LogEventKind,
    PendingState,
    RejectedState,
    ToolCall,
    WithdrawnDetail,
    WithdrawnState,
)

_DETAIL_TA: TypeAdapter[LogEventDetail] = TypeAdapter(LogEventDetail)

_STATE_TA: TypeAdapter[ActionState] = TypeAdapter(ActionState)


def test_pending_state_roundtrip():
    state = PendingState()
    json_bytes = _STATE_TA.dump_json(state)
    parsed = _STATE_TA.validate_json(json_bytes)
    assert isinstance(parsed, PendingState)
    assert parsed.status == ActionStatus.PENDING


def test_done_state_succeeded_roundtrip():
    outcome = CallToolResult(content=[TextContent(type="text", text="hello")], isError=False)
    state = DoneState(outcome=outcome)
    json_bytes = _STATE_TA.dump_json(state)
    parsed = _STATE_TA.validate_json(json_bytes)
    assert isinstance(parsed, DoneState)
    assert isinstance(parsed.outcome, CallToolResult)
    assert not parsed.outcome.isError


def test_done_state_failed_roundtrip():
    outcome = CallToolResult(content=[TextContent(type="text", text="something went wrong")], isError=True)
    state = DoneState(outcome=outcome)
    json_bytes = _STATE_TA.dump_json(state)
    parsed = _STATE_TA.validate_json(json_bytes)
    assert isinstance(parsed, DoneState)
    assert isinstance(parsed.outcome, CallToolResult)
    assert parsed.outcome.isError


def test_rejected_state_with_reason():
    state = RejectedState(reason="not today")
    json_bytes = _STATE_TA.dump_json(state)
    parsed = _STATE_TA.validate_json(json_bytes)
    assert isinstance(parsed, RejectedState)
    assert parsed.reason == "not today"


def test_rejected_state_no_reason():
    state = RejectedState()
    json_bytes = _STATE_TA.dump_json(state)
    parsed = _STATE_TA.validate_json(json_bytes)
    assert isinstance(parsed, RejectedState)
    assert parsed.reason is None


def test_withdrawn_state_roundtrip():
    state = WithdrawnState()
    json_bytes = _STATE_TA.dump_json(state)
    parsed = _STATE_TA.validate_json(json_bytes)
    assert isinstance(parsed, WithdrawnState)


def test_executing_state_roundtrip():
    state = ExecutingState()
    json_bytes = _STATE_TA.dump_json(state)
    parsed = _STATE_TA.validate_json(json_bytes)
    assert isinstance(parsed, ExecutingState)


@pytest.mark.parametrize(
    "state",
    [
        PendingState(),
        ExecutingState(),
        DoneState(outcome=CallToolResult(content=[])),
        RejectedState(reason="x"),
        WithdrawnState(),
    ],
)
def test_action_state_discriminator(state: ActionState):
    """All state variants are correctly discriminated on the status field."""
    roundtripped = _STATE_TA.validate_json(_STATE_TA.dump_json(state))
    assert roundtripped.status == state.status


def test_tool_call_extra_fields_rejected():
    with pytest.raises(ValidationError):
        ToolCall(server_namespace="test", tool_name="x", arguments={}, extra_field="oops")  # type: ignore[call-arg]


def test_action_key_roundtrip():
    key = ActionKey(session_key="sess-abc", action_seq=42)
    parsed = ActionKey.model_validate_json(key.model_dump_json())
    assert parsed.session_key == "sess-abc"
    assert parsed.action_seq == 42


def test_action_key_extra_fields_rejected():
    with pytest.raises(ValidationError):
        ActionKey(session_key="s", action_seq=1, extra="oops")  # type: ignore[call-arg]


def test_log_entry_roundtrip():
    entry = LogEntry(
        entry_id=3,
        session_key="sess-1",
        action_seq=2,
        detail=ApprovedDetail(),
        timestamp=datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC),
    )
    parsed = LogEntry.model_validate_json(entry.model_dump_json())
    assert parsed.entry_id == 3
    assert parsed.session_key == "sess-1"
    assert parsed.action_seq == 2
    assert isinstance(parsed.detail, ApprovedDetail)
    assert parsed.detail.kind == LogEventKind.APPROVED


def test_log_entry_with_denied_detail():
    entry = LogEntry(
        entry_id=1,
        session_key="s",
        action_seq=1,
        detail=DeniedDetail(reason="nope"),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    parsed = LogEntry.model_validate_json(entry.model_dump_json())
    assert isinstance(parsed.detail, DeniedDetail)
    assert parsed.detail.reason == "nope"


def test_log_entry_with_execution_finished_detail():
    outcome = CallToolResult(content=[TextContent(type="text", text="done")])
    entry = LogEntry(
        entry_id=5,
        session_key="s",
        action_seq=1,
        detail=ExecutionFinishedDetail(outcome=outcome),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    parsed = LogEntry.model_validate_json(entry.model_dump_json())
    assert isinstance(parsed.detail, ExecutionFinishedDetail)
    content_item = parsed.detail.outcome.content[0]
    assert isinstance(content_item, TextContent)
    assert content_item.text == "done"


@pytest.mark.parametrize(
    "detail",
    [
        ActionReceivedDetail(),
        ApprovedDetail(),
        DeniedDetail(reason="x"),
        WithdrawnDetail(),
        ExecutionStartedDetail(),
        ExecutionFinishedDetail(outcome=CallToolResult(content=[])),
    ],
)
def test_log_event_detail_discriminator(detail: LogEventDetail):
    """All detail variants are correctly discriminated on the kind field."""
    roundtripped = _DETAIL_TA.validate_json(_DETAIL_TA.dump_json(detail))
    assert roundtripped.kind == detail.kind


if __name__ == "__main__":
    pytest_bazel.main()
