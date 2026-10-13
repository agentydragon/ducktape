"""Strict rule parsing: what this service refuses, and the typed shapes it hands the evaluator."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane import policy_sets
from agentplane.action_service.policies.exact_actions import ExactActions
from agentplane.action_service.policies.resources import parse_action_rule

METADATA = {
    "name": "test-object",
    "namespace": "agentplane-test",
    "uid": "uid-1",
    "generation": 3,
    "resourceVersion": "7",
}


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
def test_a_rule_this_service_cannot_parse_refuses_its_set_with_the_report(policy: dict[str, Any], located: str) -> None:
    refused = policy_sets.parse_policy_set(
        {"metadata": METADATA, "spec": {"rules": [rule(policy)]}}, parse_rule=parse_action_rule
    )
    assert isinstance(refused, policy_sets.RefusedPolicySet)
    assert (refused.name, refused.generation) == ("test-object", 3)
    assert located in refused.reason


def test_a_rule_with_an_unknown_key_is_not_accepted() -> None:
    with pytest.raises(ValidationError, match="extra"):
        parse_action_rule({**rule({"type": "exact_actions", "actions": {"github": ["x"]}}), "extra": 1})


def test_another_readers_rule_is_skipped_and_an_auto_approve_rule_yields_its_policy() -> None:
    assert parse_action_rule({"type": "kubernetes_cluster_role_binding", "clusterRole": "test-reader"}) is None
    assert isinstance(parse_action_rule(rule({"type": "exact_actions", "actions": {"github": ["x"]}})), ExactActions)


if __name__ == "__main__":
    pytest_bazel.main()
