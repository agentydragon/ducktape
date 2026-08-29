"""Tests for the shared tool-call wire contract."""

from __future__ import annotations

import json

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.tool_calls import ApprovalDecision, ApprovalDecisionRequest


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


@pytest.mark.parametrize("decision", [ApprovalDecision.APPROVE, ApprovalDecision.DENY])
def test_decision_note_is_shared_by_both_operator_decisions(decision: ApprovalDecision) -> None:
    request = ApprovalDecisionRequest(decision=decision, decision_note="  reviewed  ")
    assert request.decision_note == "reviewed"
    assert request.model_dump(mode="json", exclude_none=True) == {
        "decision": decision.value,
        "decision_note": "reviewed",
    }


def test_decision_note_blank_is_normalized_to_none() -> None:
    request = ApprovalDecisionRequest(decision=ApprovalDecision.DENY, decision_note=" \t ")
    assert request.decision_note is None
    assert request.model_dump(mode="json", exclude_none=True) == {"decision": "deny"}


def test_legacy_reason_is_normalized_during_expand_release() -> None:
    request = ApprovalDecisionRequest(decision=ApprovalDecision.DENY, reason="old client")
    assert request.decision_note == "old client"


def test_decision_note_and_legacy_reason_cannot_both_be_supplied() -> None:
    with pytest.raises(ValidationError, match="cannot both be supplied"):
        ApprovalDecisionRequest(decision=ApprovalDecision.DENY, decision_note="new", reason="old")


def test_decision_note_has_a_bounded_length() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(decision=ApprovalDecision.DENY, decision_note="x" * 4097)


if __name__ == "__main__":
    pytest_bazel.main()
