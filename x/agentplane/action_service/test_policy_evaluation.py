"""Each policy kind's own test, and how a caller's bindings resolve from the index at one instant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_bazel
from pydantic import JsonValue

from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.models import SandboxCaller, ServiceAccountCaller, ServiceAccountRef
from x.agentplane.action_service.policy_evaluation import Matched, NotMatched, evaluate, resolve_bindings
from x.agentplane.action_service.policy_informer import PolicyIndex, namespaced_key
from x.agentplane.action_service.policy_resources import (
    ActionPolicyBinding,
    ActionPolicySet,
    InvalidResource,
    ObjectMeta,
    Status,
    parse_binding,
    parse_policy_set,
)

NAMESPACE = "agentplane-test"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SANDBOX = SandboxCaller(namespace=NAMESPACE, sandbox_uid="sandbox-uid-1")
ACCOUNT = ServiceAccountRef(namespace=NAMESPACE, name="test-caller")
CALLER = ServiceAccountCaller(service_account=ACCOUNT, grant_revision=1)
ECHO = ActionIdentity(group="everything", name="echo")

SCHEMA_POLICY: dict[str, Any] = {
    "type": "argument_schema",
    "actions": {"everything": ["echo"]},
    "schema": {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string", "maxLength": 5}, "count": {"type": "integer"}},
        "additionalProperties": False,
    },
}


def _meta(name: str, *, generation: int = 1, version: str = "1") -> dict[str, Any]:
    return {
        "name": name,
        "namespace": NAMESPACE,
        "uid": f"uid-{name}",
        "generation": generation,
        "resourceVersion": version,
    }


def policy_set(name: str, policies: list[dict[str, Any]], **meta: Any) -> ActionPolicySet | InvalidResource:
    return parse_policy_set({"metadata": _meta(name, **meta), "spec": {"autoApproveIf": policies}})


def binding(
    name: str, subject: dict[str, Any], sets: list[str], *, expires_at: str | None = None, **meta: Any
) -> ActionPolicyBinding | InvalidResource:
    spec: dict[str, Any] = {"subject": subject, "policySets": sets}
    if expires_at is not None:
        spec["expiresAt"] = expires_at
    return parse_binding({"metadata": _meta(name, **meta), "spec": spec})


def index_of(*objects: ActionPolicySet | ActionPolicyBinding | InvalidResource, synced: bool = True) -> PolicyIndex:
    index = PolicyIndex(synced=synced)
    for obj in objects:
        key = namespaced_key(obj.metadata.namespace, obj.metadata.name)
        if isinstance(obj, ActionPolicySet) or (
            isinstance(obj, InvalidResource) and obj.metadata.name.startswith("set")
        ):
            index.policy_sets[key] = obj
        else:
            index.bindings[key] = obj
    return index


@pytest.mark.parametrize(
    ("arguments", "matched"),
    [
        ({"message": "hi"}, True),
        ({"message": "hi", "count": 2}, True),
        ({}, False),  # `required` decides presence; `properties` alone never does.
        ({"message": "too long"}, False),
        ({"message": "hi", "count": "2"}, False),
        ({"message": "hi", "extra": True}, False),
    ],
)
def test_argument_schema_has_plain_json_schema_semantics(arguments: dict[str, JsonValue], matched: bool) -> None:
    parsed = policy_set("set-a", [SCHEMA_POLICY])
    assert isinstance(parsed, ActionPolicySet)
    (policy,) = parsed.spec.auto_approve_if
    assert isinstance(evaluate(policy, ECHO, arguments), Matched if matched else NotMatched)


@pytest.mark.parametrize(
    ("action", "matched"),
    [
        (ECHO, True),
        (ActionIdentity(group="everything", name="add"), False),
        (ActionIdentity(group="other", name="echo"), False),
    ],
)
def test_exact_actions_matches_by_name_alone(action: ActionIdentity, matched: bool) -> None:
    parsed = policy_set("set-a", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}])
    assert isinstance(parsed, ActionPolicySet)
    (policy,) = parsed.spec.auto_approve_if
    assert isinstance(evaluate(policy, action, {"anything": [1, 2]}), Matched if matched else NotMatched)


def test_resolution_takes_unexpired_valid_bindings_naming_the_caller_with_their_existing_sets() -> None:
    reads = policy_set("set-reads", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}])
    broken = policy_set("set-broken", [{"type": "nope", "actions": {"everything": ["echo"]}}])
    index = index_of(
        reads,
        broken,
        binding(
            "b-sandbox",
            {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}},
            ["set-reads", "set-broken", "set-missing"],
        ),
        binding("b-account", {"serviceAccount": {"namespace": NAMESPACE, "name": ACCOUNT.name}}, ["set-reads"]),
        binding(
            "b-expired",
            {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}},
            ["set-reads"],
            expires_at=(NOW - timedelta(seconds=1)).isoformat(),
        ),
        binding(
            "b-later",
            {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}},
            ["set-reads"],
            expires_at=(NOW + timedelta(hours=1)).isoformat(),
        ),
        binding("b-other-uid", {"sandbox": {"name": "coder", "uid": "someone-else"}}, ["set-reads"]),
        binding("b-invalid", {"sandbox": {"name": "coder"}}, ["set-reads"]),
    )
    for_sandbox = resolve_bindings(index, SANDBOX, NOW)
    assert [resolved.binding.metadata.name for resolved in for_sandbox] == ["b-later", "b-sandbox"]
    assert [[s.metadata.name for s in resolved.policy_sets] for resolved in for_sandbox] == [
        ["set-reads"],
        ["set-reads"],
    ]
    for_account = resolve_bindings(index, CALLER, NOW)
    assert [resolved.binding.metadata.name for resolved in for_account] == ["b-account"]
    assert (
        resolve_bindings(index, SandboxCaller(namespace="agentplane-other", sandbox_uid=SANDBOX.sandbox_uid), NOW) == ()
    )
    assert (
        resolve_bindings(
            index,
            ServiceAccountCaller(service_account=ServiceAccountRef(namespace=NAMESPACE, name="x"), grant_revision=3),
            NOW,
        )
        == ()
    )


def test_nothing_resolves_before_the_informer_has_synced() -> None:
    index = index_of(
        policy_set("set-reads", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}]),
        binding("b-sandbox", {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}}, ["set-reads"]),
        synced=False,
    )
    assert resolve_bindings(index, SANDBOX, NOW) == ()
    index.synced = True
    assert len(resolve_bindings(index, SANDBOX, NOW)) == 1


def test_invalid_resource_keeps_metadata_for_status_reporting() -> None:
    broken = binding("b-invalid", {"sandbox": {"name": "coder"}}, ["set-reads"])
    assert isinstance(broken, InvalidResource)
    assert broken.metadata == ObjectMeta(
        name="b-invalid", namespace=NAMESPACE, uid="uid-b-invalid", generation=1, resource_version="1"
    )
    assert broken.status == Status()


if __name__ == "__main__":
    pytest_bazel.main()
