"""Strict spec parsing: what this service refuses, and the typed shapes it hands the evaluator."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel

from x.agentplane.action_service.models import PolicyKind
from x.agentplane.action_service.policies.argument_schema import ArgumentSchema
from x.agentplane.action_service.policies.exact_actions import ExactActions
from x.agentplane.action_service.policies.github_public_repository import GitHubPublicRepository
from x.agentplane.action_service.policies.github_repository import GitHubRepository
from x.agentplane.action_service.policies.resources import (
    ActionPolicyBinding,
    ActionPolicySet,
    InvalidResource,
    SandboxSubject,
    ServiceAccountSubject,
    parse_binding,
    parse_policy_set,
)

METADATA = {
    "name": "test-object",
    "namespace": "agentplane-test",
    "uid": "uid-1",
    "generation": 3,
    "resourceVersion": "7",
}


def policy_set(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "agentplane.allegedly.works/v1alpha1",
        "kind": "ActionPolicySet",
        "metadata": METADATA,
        "spec": spec,
    }


def binding(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "agentplane.allegedly.works/v1alpha1",
        "kind": "ActionPolicyBinding",
        "metadata": METADATA,
        "spec": spec,
    }


def test_policy_set_parses_each_kind_and_keeps_deny_lists() -> None:
    parsed = parse_policy_set(
        policy_set(
            {
                "autoApproveIf": [
                    {"type": "exact_actions", "actions": {"github": ["get_file_contents", "search_code"]}},
                    {
                        "type": "argument_schema",
                        "actions": {"github": ["create_issue"]},
                        "schema": {"properties": {"owner": {"const": "test-owner"}}, "required": ["owner"]},
                    },
                    {
                        "type": "github_repository",
                        "actions": {"github": ["get_file_contents"]},
                        "owner": "test-owner",
                        "repository": "test-repo",
                    },
                    {"type": "github_public_repository", "actions": {"github": ["get_file_contents"]}},
                ],
                "autoDenyIf": [{"type": "exact_actions", "actions": {"ssh": ["run"]}}],
            }
        )
    )
    assert isinstance(parsed, ActionPolicySet)
    exact, by_schema, fixed_repository, public_repository = parsed.spec.auto_approve_if
    assert isinstance(exact, ExactActions)
    assert exact.actions == {"github": frozenset({"get_file_contents", "search_code"})}
    assert isinstance(by_schema, ArgumentSchema)
    assert by_schema.argument_schema["required"] == ["owner"]
    assert isinstance(fixed_repository, GitHubRepository)
    assert (fixed_repository.owner, fixed_repository.repository) == ("test-owner", "test-repo")
    assert isinstance(public_repository, GitHubPublicRepository)
    assert [policy.type for policy in parsed.spec.auto_deny_if] == [PolicyKind.EXACT_ACTIONS]
    assert parsed.spec.auto_deny_unless == []
    assert parsed.metadata.generation == 3
    assert parsed.status.ready() is None


@pytest.mark.parametrize(
    ("spec", "located"),
    [
        ({"autoApproveIf": [{"type": "allow_all", "actions": {"github": ["x"]}}]}, "autoApproveIf.0"),
        ({"autoApproveIf": [{"type": "exact_actions"}]}, "actions"),
        ({"autoApproveIf": [{"type": "exact_actions", "actions": {"github": []}}]}, "actions"),
        ({"autoApproveIf": [{"type": "exact_actions", "actions": {"github": ["x"]}, "extra": 1}]}, "extra"),
        (
            {"autoApproveIf": [{"type": "argument_schema", "actions": {"github": ["x"]}, "schema": {"type": "nope"}}]},
            "not a valid JSON Schema",
        ),
        ({"autoApproveIf": [{"type": "argument_schema", "actions": {"github": ["x"]}}]}, "schema"),
        ({"autoApproveIf": [{"type": "github_repository", "actions": {"github": ["x"]}, "owner": "o"}]}, "repository"),
        (
            {"autoApproveIf": [{"type": "github_public_repository", "actions": {"github": ["x"]}, "owner": "o"}]},
            "owner",
        ),
        ({"autoApproveIf": [{"type": "exact_actions", "actions": {"github": ["Not-A-Key"]}}]}, "actions"),
        ({"rules": []}, "rules"),
    ],
)
def test_invalid_policy_set_is_kept_with_its_report(spec: dict[str, Any], located: str) -> None:
    parsed = parse_policy_set(policy_set(spec))
    assert isinstance(parsed, InvalidResource)
    assert parsed.metadata.name == "test-object"
    assert located in parsed.message


def test_binding_subject_is_one_of_service_account_or_sandbox() -> None:
    by_account = parse_binding(
        binding(
            {"subject": {"serviceAccount": {"namespace": "agentplane-test", "name": "caller"}}, "policySets": ["a"]}
        )
    )
    assert isinstance(by_account, ActionPolicyBinding)
    assert isinstance(by_account.spec.subject, ServiceAccountSubject)
    assert by_account.spec.subject.service_account.name == "caller"
    assert by_account.spec.expires_at is None
    by_sandbox = parse_binding(
        binding(
            {
                "subject": {"sandbox": {"name": "coder-1", "uid": "sandbox-uid-1"}},
                "policySets": ["a", "b"],
                "expiresAt": "2026-09-12T20:00:00Z",
            }
        )
    )
    assert isinstance(by_sandbox, ActionPolicyBinding)
    assert isinstance(by_sandbox.spec.subject, SandboxSubject)
    assert by_sandbox.spec.subject.sandbox.uid == "sandbox-uid-1"
    assert by_sandbox.spec.expires_at is not None
    assert by_sandbox.spec.expires_at.tzinfo is not None


@pytest.mark.parametrize(
    "spec",
    [
        {"subject": {}, "policySets": ["a"]},
        {
            "subject": {
                "serviceAccount": {"namespace": "agentplane-test", "name": "caller"},
                "sandbox": {"name": "coder-1", "uid": "u"},
            },
            "policySets": ["a"],
        },
        {"subject": {"sandbox": {"name": "coder-1"}}, "policySets": ["a"]},
        {"subject": {"sandbox": {"name": "coder-1", "uid": "u"}}, "policySets": []},
        {"subject": {"sandbox": {"name": "coder-1", "uid": "u"}}, "policySets": ["a"], "expiresAt": "tomorrow"},
        {
            "subject": {"sandbox": {"name": "coder-1", "uid": "u"}},
            "policySets": ["a"],
            "expiresAt": "2026-09-12T20:00:00",
        },
    ],
)
def test_invalid_binding_is_kept_with_its_report(spec: dict[str, Any]) -> None:
    parsed = parse_binding(binding(spec))
    assert isinstance(parsed, InvalidResource)
    assert parsed.message


if __name__ == "__main__":
    pytest_bazel.main()
