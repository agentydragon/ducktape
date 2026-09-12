"""How a caller's bindings resolve from the index at one instant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_bazel

from x.agentplane.action_service.models import SandboxCaller, ServiceAccountCaller, ServiceAccountRef
from x.agentplane.action_service.policies.resources import (
    ActionPolicyBinding,
    ActionPolicySet,
    InvalidResource,
    ObjectMeta,
    Status,
    parse_binding,
    parse_policy_set,
)
from x.agentplane.action_service.policy_evaluation import resolve_bindings
from x.agentplane.action_service.policy_informer import PolicyIndex, namespaced_key

NAMESPACE = "agentplane-test"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SANDBOX = SandboxCaller(namespace=NAMESPACE, sandbox_uid="sandbox-uid-1")
ACCOUNT = ServiceAccountRef(namespace=NAMESPACE, name="test-caller")
CALLER = ServiceAccountCaller(service_account=ACCOUNT, grant_revision=1)


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
