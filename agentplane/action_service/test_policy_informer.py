"""The informer against a real API server: sync, ActionsReady status writes, label changes, deletion, relists."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api, CustomObjectsApi
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane import policy_sets
from agentplane.action_service.models import NamespacedName
from agentplane.action_service.policies.resources import ACTIONS_READY_CONDITION, CALLER_LABEL
from agentplane.action_service.policy_informer import PolicyIndex, PolicyInformer
from agentplane.crd_group import GROUP, VERSION
from agentplane.kubernetes_watch import Freshness
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, control_plane, kubernetes_api
from util.kubernetes import CustomObjectsClient

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


def policy_set(name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicySet",
        "metadata": {"name": name},
        "spec": {"rules": rules},
    }


@dataclass(frozen=True)
class Policies:
    """One test's namespace, holding its policy objects and its callers' ServiceAccounts."""

    server: KubernetesApiServer
    namespace: str

    @property
    def custom_objects(self) -> CustomObjectsClient:
        return cast(CustomObjectsClient, CustomObjectsApi(self.server.api))

    @property
    def core(self) -> CoreV1Api:
        return CoreV1Api(self.server.api)

    async def create(self, plural: str, body: dict[str, Any]) -> None:
        await self.custom_objects.create_namespaced_custom_object(GROUP, VERSION, self.namespace, plural, body)

    async def binding(self) -> dict[str, Any]:
        return await self.custom_objects.get_namespaced_custom_object(
            GROUP, VERSION, self.namespace, policy_sets.POLICY_BINDINGS_PLURAL, POLICY_BINDING
        )

    async def label(self, account: str, *, labeled: bool) -> None:
        await self.core.patch_namespaced_service_account(
            account, self.namespace, {"metadata": {"labels": {CALLER_LABEL: "true" if labeled else None}}}
        )

    async def seed(self) -> None:
        await self.core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=self.namespace)))
        await self.create(
            policy_sets.POLICY_SETS_PLURAL,
            policy_set(
                RULES_SET,
                [
                    {"type": "kubernetes_cluster_role_binding", "clusterRole": "test-reader"},
                    {"type": "action_auto_approve", "policy": {"type": "exact_actions", "actions": {"g": ["a"]}}},
                ],
            ),
        )
        # Admitted by the CRD, refused by this service: the schema is no JSON Schema.
        await self.create(
            policy_sets.POLICY_SETS_PLURAL,
            policy_set(
                REFUSING_SET,
                [
                    {
                        "type": "action_auto_approve",
                        "policy": {"type": "argument_schema", "actions": {"g": ["a"]}, "schema": {"type": "nope"}},
                    }
                ],
            ),
        )
        await self.create(
            policy_sets.POLICY_BINDINGS_PLURAL,
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "PolicyBinding",
                "metadata": {"name": POLICY_BINDING},
                "spec": {
                    "subject": {"namespace": self.namespace, "name": CALLER},
                    "policySets": [RULES_SET, REFUSING_SET, "test-missing"],
                },
            },
        )
        # Another reader's condition already on the binding, which this service's writes must keep.
        await self.custom_objects.patch_namespaced_custom_object_status(
            GROUP,
            VERSION,
            self.namespace,
            policy_sets.POLICY_BINDINGS_PLURAL,
            POLICY_BINDING,
            {"status": {"conditions": [KUBERNETES_READY]}},
            _content_type="application/merge-patch+json",
        )
        for account, labels in ((CALLER, {CALLER_LABEL: "true"}), (UNLABELED, {"other": "label"})):
            await self.core.create_namespaced_service_account(
                self.namespace,
                k8s_client.V1ServiceAccount(metadata=k8s_client.V1ObjectMeta(name=account, labels=labels)),
            )


@pytest.fixture(scope="session")
def kubeconfig() -> Iterator[Path]:
    """One control plane for the whole test target; each test's objects are in a namespace of its own."""
    with control_plane() as path:
        yield path


@pytest.fixture
async def policies(kubeconfig: Path) -> AsyncIterator[Policies]:
    async with kubernetes_api(kubeconfig) as server:
        seeded = Policies(server, f"policy-{uuid4().hex[:8]}-test")
        await seeded.seed()
        yield seeded


@pytest.fixture
def resync_seconds() -> int:
    """The informer's watch period, which a test that needs a watch to end parametrizes shorter."""
    return 60


@pytest.fixture
async def index(policies: Policies, resync_seconds: int) -> AsyncIterator[PolicyIndex]:
    index = PolicyIndex(freshness=Freshness(stale_after_seconds=900))
    informer = PolicyInformer(
        index=index,
        custom_objects=policies.custom_objects,
        core_v1=policies.core,
        policy_namespace=policies.namespace,
        caller_service_account_namespaces={policies.namespace},
        resync_seconds=resync_seconds,
    )
    task = asyncio.create_task(informer.run())
    try:
        await index.wait_for(lambda: index.synced)
        yield index
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_initial_sync_indexes_rule_sets_bindings_and_only_labeled_callers(
    policies: Policies, index: PolicyIndex
) -> None:
    assert isinstance(index.rule_sets[RULES_SET], policy_sets.PolicySet)
    assert isinstance(index.rule_sets[REFUSING_SET], policy_sets.RefusedPolicySet)
    caller = ServiceAccountRef(namespace=policies.namespace, name=CALLER)
    assert index.policy_bindings[POLICY_BINDING].spec.subject == caller
    assert index.caller_service_accounts() == [caller]
    assert index.admits(caller)
    assert not index.admits(ServiceAccountRef(namespace=policies.namespace, name=UNLABELED))
    assert not index.admits(ServiceAccountRef(namespace="test-other-namespace", name=CALLER))


async def settled_actions_ready(policies: Policies, status: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Once the stored binding's ActionsReady reads `status`: that condition and all the binding's conditions."""
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
        with attempt:
            conditions = (await policies.binding()).get("status", {}).get("conditions", [])
            ready = next((c for c in conditions if c["type"] == ACTIONS_READY_CONDITION), None)
            assert ready is not None
            assert ready["status"] == status
            return ready, conditions
    raise AssertionError("ActionsReady retry exhausted")


async def test_actions_ready_names_unavailable_sets_and_keeps_other_readers_conditions(
    policies: Policies, index: PolicyIndex
) -> None:
    ready, conditions = await settled_actions_ready(policies, "False")
    assert ready["reason"] == "PolicySetUnavailable"
    assert "test-missing" in ready["message"]
    assert REFUSING_SET in ready["message"]
    assert RULES_SET not in ready["message"]
    assert KUBERNETES_READY in conditions

    await policies.custom_objects.patch_namespaced_custom_object(
        GROUP,
        VERSION,
        policies.namespace,
        policy_sets.POLICY_BINDINGS_PLURAL,
        POLICY_BINDING,
        {"spec": {"policySets": [RULES_SET]}},
        _content_type="application/merge-patch+json",
    )
    _, conditions = await settled_actions_ready(policies, "True")
    assert KUBERNETES_READY in conditions


async def test_label_removal_and_deletion_reach_the_index(policies: Policies, index: PolicyIndex) -> None:
    await policies.label(CALLER, labeled=False)
    await index.wait_for(lambda: NamespacedName(policies.namespace, CALLER) not in index.service_accounts)
    assert not index.admits(ServiceAccountRef(namespace=policies.namespace, name=CALLER))
    await policies.label(UNLABELED, labeled=True)
    await index.wait_for(lambda: NamespacedName(policies.namespace, UNLABELED) in index.service_accounts)
    await policies.custom_objects.delete_namespaced_custom_object(
        GROUP,
        VERSION,
        policies.namespace,
        policy_sets.POLICY_BINDINGS_PLURAL,
        POLICY_BINDING,
        body=k8s_client.V1DeleteOptions(),
    )
    await index.wait_for(lambda: POLICY_BINDING not in index.policy_bindings)


@pytest.mark.parametrize("resync_seconds", [1])
async def test_watch_end_relists(policies: Policies, index: PolicyIndex) -> None:
    """Once the server ends a watch on schedule, the relist and the watch after it keep the index current."""
    listed = index.freshness.at[policy_sets.POLICY_SETS_PLURAL]
    # A later stamp is only recorded past a watch the server ended at `timeoutSeconds`.
    await index.wait_for(lambda: index.freshness.at[policy_sets.POLICY_SETS_PLURAL] > listed)
    await policies.create(
        policy_sets.POLICY_SETS_PLURAL,
        policy_set("test-late", [{"type": "kubernetes_cluster_role_binding", "clusterRole": "test-reader"}]),
    )
    await index.wait_for(lambda: "test-late" in index.rule_sets)


if __name__ == "__main__":
    pytest_bazel.main()
