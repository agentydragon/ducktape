"""Tests for approval_gate discriminated union models."""

from __future__ import annotations

import pytest
import pytest_bazel
from mcp.types import CallToolResult, TextContent
from pydantic import TypeAdapter, ValidationError

from approval_gate.models import (
    ActionState,
    ActionStatus,
    DoneState,
    ExecutingState,
    PendingState,
    RejectedState,
    ToolCall,
    WithdrawnState,
)

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


if __name__ == "__main__":
    pytest_bazel.main()
