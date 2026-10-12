"""The informer against the fake API server: sync, Ready status writes, label changes, deletion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi

from agentplane import policy_sets
from agentplane.action_service.models import NamespacedName
from agentplane.action_service.policies.resources import (
    ACTIONS_READY_CONDITION,
    BINDINGS_PLURAL,
    CALLER_LABEL,
    POLICY_SETS_PLURAL,
    SERVICE_ACCOUNTS_PLURAL,
    ActionPolicyBinding,
    ActionPolicySet,
    InvalidResource,
)
from agentplane.action_service.policy_informer import PolicyIndex, PolicyInformer
from agentplane.crd_group import GROUP, VERSION
from agentplane.kubernetes_watch import Freshness
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import FakeApiServer, fake_apiserver
from util.kubernetes import CustomObjectsClient

NAMESPACE = "agentplane-policy-test"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
VALID_SET = "reads"
GITHUB_SET = "github-reads"
INVALID_SET = "broken"
BINDING = "caller-reads"
RULES_SET = "test-rules"
REFUSING_SET = "test-refused-rules"
POLICY_BINDING = "test-policy-binding"
CALLER = "test-caller"
UNLABELED = "test-bystander"
KUBERNETES_READY = {
    "type": "KubernetesReady",
    "status": "True",
    "reason": "Applied",
    "message": "test",
    "lastTransitionTime": "2026-09-12T11:00:00Z",
}


def policy_set(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "ActionPolicySet",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": spec,
    }


def binding(name: str, subject: dict[str, Any], policy_sets: list[str]) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "ActionPolicyBinding",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"subject": subject, "policySets": policy_sets},
    }


def generic_set(name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicySet",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"rules": rules},
    }


def policy_binding(policy_sets: list[str]) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicyBinding",
        "metadata": {"name": POLICY_BINDING, "namespace": NAMESPACE},
        "spec": {"subject": {"namespace": NAMESPACE, "name": CALLER}, "policySets": policy_sets},
    }


def service_account(name: str, *, labeled: bool) -> dict[str, Any]:
    labels = {CALLER_LABEL: "true"} if labeled else {"other": "label"}
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
    }


def seed(fake: FakeApiServer) -> None:
    fake.put(
        POLICY_SETS_PLURAL,
        policy_set(VALID_SET, {"autoApproveIf": [{"type": "exact_actions", "actions": {"g": ["a"]}}]}),
    )
    fake.put(
        POLICY_SETS_PLURAL,
        policy_set(
            GITHUB_SET,
            {
                "autoApproveIf": [
                    {"type": "github_repository", "actions": {"g": ["a"]}, "owner": "test-owner", "repository": "r"},
                    {"type": "github_public_repository", "actions": {"g": ["a"]}},
                ]
            },
        ),
    )
    fake.put(
        POLICY_SETS_PLURAL,
        policy_set(INVALID_SET, {"autoApproveIf": [{"type": "no_such_kind", "actions": {"g": ["a"]}}]}),
    )
    fake.put(BINDINGS_PLURAL, binding(BINDING, {"namespace": NAMESPACE, "name": CALLER}, [VALID_SET]))
    fake.put(
        policy_sets.POLICY_SETS_PLURAL,
        generic_set(
            RULES_SET,
            [
                {"type": "kubernetes_cluster_role_binding", "clusterRole": "test-reader"},
                {"type": "action_auto_approve", "policy": {"type": "exact_actions", "actions": {"g": ["a"]}}},
            ],
        ),
    )
    fake.put(
        policy_sets.POLICY_SETS_PLURAL,
        generic_set(REFUSING_SET, [{"type": "action_auto_approve", "policy": {"type": "no_such_kind", "actions": {}}}]),
    )
    # Another reader's condition already on the binding, which this service's writes must keep.
    fake.put(
        policy_sets.POLICY_BINDINGS_PLURAL,
        {**policy_binding([RULES_SET, REFUSING_SET, "test-missing"]), "status": {"conditions": [KUBERNETES_READY]}},
    )
    fake.put(SERVICE_ACCOUNTS_PLURAL, service_account(CALLER, labeled=True))
    fake.put(SERVICE_ACCOUNTS_PLURAL, service_account(UNLABELED, labeled=False))


@pytest.fixture
async def fake() -> AsyncIterator[FakeApiServer]:
    async with fake_apiserver(
        namespace_of={
            POLICY_SETS_PLURAL: NAMESPACE,
            BINDINGS_PLURAL: NAMESPACE,
            SERVICE_ACCOUNTS_PLURAL: NAMESPACE,
            policy_sets.POLICY_SETS_PLURAL: NAMESPACE,
            policy_sets.POLICY_BINDINGS_PLURAL: NAMESPACE,
        }
    ) as server:
        seed(server)
        yield server


@pytest.fixture
async def index(fake: FakeApiServer) -> AsyncIterator[PolicyIndex]:
    configuration = k8s_client.Configuration(host=f"http://127.0.0.1:{fake.port}")
    async with ApiClient(configuration=configuration) as api:
        index = PolicyIndex(freshness=Freshness(stale_after_seconds=900), clock=lambda: NOW)
        informer = PolicyInformer(
            index=index,
            custom_objects=cast(CustomObjectsClient, CustomObjectsApi(api)),
            core_v1=CoreV1Api(api),
            policy_namespace=NAMESPACE,
            caller_service_account_namespaces={NAMESPACE},
            resync_seconds=60,
        )
        task = asyncio.create_task(informer.run())
        try:
            await index.wait_for(lambda: index.synced)
            yield index
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def key(name: str) -> NamespacedName:
    return NamespacedName(NAMESPACE, name)


def _ready(index: PolicyIndex, name: str, status: str, generation: int) -> bool:
    ready = index.policy_sets[key(name)].status.ready()
    return ready is not None and ready.status == status and ready.observed_generation == generation


async def test_initial_sync_keeps_invalid_objects_and_only_labeled_callers(index: PolicyIndex) -> None:
    assert isinstance(index.policy_sets[key(VALID_SET)], ActionPolicySet)
    assert isinstance(index.policy_sets[key(GITHUB_SET)], ActionPolicySet)
    broken = index.policy_sets[key(INVALID_SET)]
    assert isinstance(broken, InvalidResource)
    assert "autoApproveIf.0" in broken.message
    assert isinstance(index.bindings[key(BINDING)], ActionPolicyBinding)
    assert index.caller_service_accounts() == [ServiceAccountRef(namespace=NAMESPACE, name=CALLER)]
    assert index.admits(ServiceAccountRef(namespace=NAMESPACE, name=CALLER))
    assert not index.admits(ServiceAccountRef(namespace=NAMESPACE, name=UNLABELED))
    assert not index.admits(ServiceAccountRef(namespace="agentplane-other", name=CALLER))


async def test_ready_is_written_once_per_generation_and_names_the_fault(
    fake: FakeApiServer, index: PolicyIndex
) -> None:
    await index.wait_for(
        lambda: (
            _ready(index, VALID_SET, "True", 1)
            and _ready(index, GITHUB_SET, "True", 1)
            and _ready(index, INVALID_SET, "False", 1)
            and index.bindings[key(BINDING)].status.ready() is not None
        )
    )
    written = {name: patch["conditions"][0] for name, patch in fake.status_patches}
    assert written[VALID_SET]["reason"] == "Valid"
    assert written[GITHUB_SET]["reason"] == "Valid"
    assert written[INVALID_SET]["reason"] == "Invalid"
    assert "no_such_kind" in written[INVALID_SET]["message"]
    assert written[INVALID_SET]["observedGeneration"] == 1
    assert written[VALID_SET]["lastTransitionTime"] == "2026-09-12T12:00:00Z"
    # The MODIFIED event carrying the written status is not a reason to write it again.
    assert sorted(name for name, _ in fake.status_patches if name != POLICY_BINDING) == sorted(
        [BINDING, GITHUB_SET, INVALID_SET, VALID_SET]
    )

    fake.put(
        POLICY_SETS_PLURAL,
        policy_set(INVALID_SET, {"autoApproveIf": [{"type": "exact_actions", "actions": {"g": ["a"]}}]}),
    )
    await index.wait_for(lambda: _ready(index, INVALID_SET, "True", 2))
    assert isinstance(index.policy_sets[key(INVALID_SET)], ActionPolicySet)
    assert [name for name, _ in fake.status_patches].count(INVALID_SET) == 2


def _actions_ready(fake: FakeApiServer) -> dict[str, Any] | None:
    conditions = (
        fake.objects[policy_sets.POLICY_BINDINGS_PLURAL][POLICY_BINDING].get("status", {}).get("conditions", [])
    )
    return next((c for c in conditions if c["type"] == ACTIONS_READY_CONDITION), None)


async def test_actions_ready_names_unavailable_sets_and_keeps_other_readers_conditions(
    fake: FakeApiServer, index: PolicyIndex
) -> None:
    await index.wait_for(lambda: (ready := _actions_ready(fake)) is not None and ready["status"] == "False")
    ready = _actions_ready(fake)
    assert ready is not None
    assert ready["reason"] == "PolicySetUnavailable"
    assert "test-missing" in ready["message"]
    assert REFUSING_SET in ready["message"]
    assert RULES_SET not in ready["message"]
    assert KUBERNETES_READY in fake.objects[policy_sets.POLICY_BINDINGS_PLURAL][POLICY_BINDING]["status"]["conditions"]

    fake.put(policy_sets.POLICY_BINDINGS_PLURAL, policy_binding([RULES_SET]))
    await index.wait_for(lambda: (ready := _actions_ready(fake)) is not None and ready["status"] == "True")
    assert KUBERNETES_READY in fake.objects[policy_sets.POLICY_BINDINGS_PLURAL][POLICY_BINDING]["status"]["conditions"]


async def test_label_removal_and_deletion_reach_the_index(fake: FakeApiServer, index: PolicyIndex) -> None:
    fake.put(SERVICE_ACCOUNTS_PLURAL, service_account(CALLER, labeled=False))
    await index.wait_for(lambda: key(CALLER) not in index.service_accounts)
    assert not index.admits(ServiceAccountRef(namespace=NAMESPACE, name=CALLER))
    fake.put(SERVICE_ACCOUNTS_PLURAL, service_account(UNLABELED, labeled=True))
    await index.wait_for(lambda: key(UNLABELED) in index.service_accounts)
    fake.delete(BINDINGS_PLURAL, BINDING)
    await index.wait_for(lambda: key(BINDING) not in index.bindings)


async def test_watch_end_relists(fake: FakeApiServer, index: PolicyIndex) -> None:
    """Changes made while no watch is open are picked up by the relist that follows."""
    fake.close_watches()
    fake.put(POLICY_SETS_PLURAL, policy_set("late", {"autoApproveIf": []}))
    await index.wait_for(lambda: key("late") in index.policy_sets)


if __name__ == "__main__":
    pytest_bazel.main()
