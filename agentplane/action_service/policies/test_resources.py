"""Strict validation of action_auto_approve PolicySet rules."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.action_service.policies.exact_actions import ExactActions
from agentplane.action_service.policies.resources import parse_action_rule


def rule(policy: dict[str, Any]) -> dict[str, Any]:
    return {"type": "action_auto_approve", "policy": policy}


@pytest.mark.parametrize(
    ("policy", "located"),
    [
        ({"type": "allow_all", "actions": {"github": ["x"]}}, "policy"),
        ({"type": "exact_actions"}, "actions"),
        ({"type": "exact_actions", "actions": {"github": []}}, "actions"),
        ({"type": "exact_actions", "actions": {"github": ["x"]}, "extra": 1}, "extra"),
        (
            {"type": "argument_schema", "actions": {"github": ["x"]}, "schema": {"type": "nope"}},
            "not a valid JSON Schema",
        ),
        ({"type": "argument_schema", "actions": {"github": ["x"]}}, "schema"),
        ({"type": "github_repository", "actions": {"github": ["x"]}, "owner": "o"}, "repository"),
        ({"type": "github_public_repository", "actions": {"github": ["x"]}, "owner": "o"}, "owner"),
        ({"type": "home_assistant_entity_control", "actions": {"ha-test": ["ha_call_service"]}}, "entities"),
        ({"type": "exact_actions", "actions": {"github": ["Not-A-Key"]}}, "actions"),
    ],
)
def test_an_invalid_action_rule_is_rejected(policy: dict[str, Any], located: str) -> None:
    with pytest.raises((ValidationError, ValueError)) as error:
        parse_action_rule(rule(policy))
    assert located in str(error.value)


def test_a_rule_with_an_unknown_key_is_not_accepted() -> None:
    with pytest.raises(ValidationError, match="extra"):
        parse_action_rule({**rule({"type": "exact_actions", "actions": {"github": ["x"]}}), "extra": 1})


def test_another_readers_rule_is_skipped_and_an_auto_approve_rule_yields_its_policy() -> None:
    assert parse_action_rule({"type": "kubernetes_cluster_role_binding", "clusterRole": "test-reader"}) is None
    assert isinstance(parse_action_rule(rule({"type": "exact_actions", "actions": {"github": ["x"]}})), ExactActions)


if __name__ == "__main__":
    pytest_bazel.main()
