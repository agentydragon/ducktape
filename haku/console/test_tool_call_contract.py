"""Tests for the shared tool-call wire contract."""

from __future__ import annotations

import json

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.tool_calls import ApprovalDecision, ApprovalDecisionRequest, ToolCallStatus


def test_tool_call_status_wire_values() -> None:
    # Pins both the spelling haku-state parses off the wire and the declaration order, which the
    # Postgres `tool_call_status` enum's label order must match (see
    # test_mcp_approval.test_fresh_baseline_enum_values_match_domain_enums). Append only.
    assert [status.value for status in ToolCallStatus] == [
        "pending_approval",
        "running",
        "ok",
        "error",
        "denied",
        "withdrawn",
    ]


@pytest.mark.parametrize(
    ("decision", "wire_value"), [(ApprovalDecision.APPROVE, "approve"), (ApprovalDecision.DENY, "deny")]
)
def test_approval_decision_wire_value(decision: ApprovalDecision, wire_value: str) -> None:
    request = ApprovalDecisionRequest(decision=decision)
    assert request.model_dump(mode="json", exclude_none=True) == {"decision": wire_value}
    assert ApprovalDecisionRequest.model_validate_json(json.dumps({"decision": wire_value})).decision is decision


def test_approval_decision_rejects_unknown_wire_value() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest.model_validate_json('{"decision":"permit"}')


if __name__ == "__main__":
    pytest_bazel.main()
