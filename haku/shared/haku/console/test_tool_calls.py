"""Tests for the shared tool-call wire contract."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.tool_calls import ApprovalDecision, ApprovalDecisionRequest


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


def test_decision_note_has_a_bounded_length() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(decision=ApprovalDecision.DENY, decision_note="x" * 4097)


if __name__ == "__main__":
    pytest_bazel.main()
