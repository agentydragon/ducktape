"""The controller against the fake API server: native bindings follow the watched PolicyBindings, and
each object reports in its `KubernetesReady` condition."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from more_itertools import one

from agentplane.crd_group import GROUP, VERSION
from agentplane.kubernetes_policy.controller import (
    KUBERNETES_READY,
    MANAGED_BY_LABEL,
    POLICY_NAMESPACE_LABEL,
    KubernetesPolicyController,
)
from agentplane.policy.resources import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.testing.fake_apiserver import FakeApiServer, fake_apiserver
from util.kubernetes import CustomObjectsClient

POLICY_NAMESPACE = "test-policies"
SUBJECT_NAMESPACE = "test-subjects"
SERVICE_ACCOUNTS_PLURAL = "serviceaccounts"
ROLE_BINDINGS_PLURAL = "rolebindings"
CLUSTER_ROLE_BINDINGS_PLURAL = "clusterrolebindings"
CLUSTER_ROLES_PLURAL = "clusterroles"
DIAGNOSTICS = "test-diagnostics"
ACCOUNT = "test-account"
GRANT = "test-grant"
NAMESPACE_READER = "test-namespace-reader"
NODE_READER = "test-node-reader"


@dataclass
class Cluster:
    fake: FakeApiServer

    def condition(self, plural: str, name: str) -> dict[str, Any] | None:
        obj = self.fake.objects[plural].get(name, {})
        conditions = obj.get("status", {}).get("conditions", [])
        return next((condition for condition in conditions if condition["type"] == KUBERNETES_READY), None)

    async def wait_for_condition(self, plural: str, name: str, status: str, reason: str) -> dict[str, Any]:
        def reported() -> bool:
            condition = self.condition(plural, name)
            return condition is not None and (condition["status"], condition["reason"]) == (status, reason)

        await self.fake.wait_for(reported)
        condition = self.condition(plural, name)
        assert condition is not None
        return condition

    def natives(self) -> list[dict[str, Any]]:
        return [
            *self.fake.objects[ROLE_BINDINGS_PLURAL].values(),
            *self.fake.objects[CLUSTER_ROLE_BINDINGS_PLURAL].values(),
        ]

    def native_roles(self) -> set[tuple[str | None, str]]:
        """(namespace, role name) of every native binding; `None` for a ClusterRoleBinding."""
        return {(native["metadata"].get("namespace"), native["roleRef"]["name"]) for native in self.natives()}

    async def wait_for_natives(self, roles: set[tuple[str | None, str]]) -> None:
        await self.fake.wait_for(lambda: self.native_roles() == roles)


def policy_set() -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicySet",
        "metadata": {"name": DIAGNOSTICS, "namespace": POLICY_NAMESPACE},
        "spec": {
            "rules": [
                {
                    "type": "kubernetes_role_binding",
                    "role": {"kind": "ClusterRole", "name": NAMESPACE_READER},
                    "namespaces": ["test-team-a", "test-team-b"],
                },
                {"type": "kubernetes_cluster_role_binding", "clusterRole": NODE_READER},
            ]
        },
    }


def cluster_role(name: str) -> dict[str, Any]:
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": name}}


def binding(*, expires_at: datetime | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"subject": {"namespace": SUBJECT_NAMESPACE, "name": ACCOUNT}, "policySets": [DIAGNOSTICS]}
    if expires_at is not None:
        spec["expiresAt"] = expires_at.isoformat()
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicyBinding",
        "metadata": {"name": GRANT, "namespace": POLICY_NAMESPACE},
        "spec": spec,
    }


GRANTED = {("test-team-a", NAMESPACE_READER), ("test-team-b", NAMESPACE_READER), (None, NODE_READER)}


@pytest.fixture
def cluster_roles() -> set[str]:
    return {NAMESPACE_READER, NODE_READER}


@pytest.fixture
async def cluster(cluster_roles: set[str]) -> AsyncIterator[Cluster]:
    async with fake_apiserver(
        namespace_of={
            POLICY_SETS_PLURAL: POLICY_NAMESPACE,
            POLICY_BINDINGS_PLURAL: POLICY_NAMESPACE,
            SERVICE_ACCOUNTS_PLURAL: SUBJECT_NAMESPACE,
            ROLE_BINDINGS_PLURAL: None,
            CLUSTER_ROLE_BINDINGS_PLURAL: None,
            CLUSTER_ROLES_PLURAL: None,
        }
    ) as fake:
        for name in cluster_roles:
            fake.put(CLUSTER_ROLES_PLURAL, cluster_role(name))
        fake.put(
            SERVICE_ACCOUNTS_PLURAL,
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": ACCOUNT, "namespace": SUBJECT_NAMESPACE},
            },
        )
        fake.put(POLICY_SETS_PLURAL, policy_set())
        async with k8s_client.ApiClient(k8s_client.Configuration(host=f"http://127.0.0.1:{fake.port}")) as api:
            controller = KubernetesPolicyController(
                custom_objects=cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api)),
                core_v1=k8s_client.CoreV1Api(api),
                rbac=k8s_client.RbacAuthorizationV1Api(api),
                policy_namespace=POLICY_NAMESPACE,
                subject_namespaces={SUBJECT_NAMESPACE},
                resync_seconds=60,
            )
            task = asyncio.create_task(controller.run())
            try:
                yield Cluster(fake)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_binding_grants_its_sets_until_deleted(cluster: Cluster) -> None:
    await cluster.wait_for_condition(POLICY_SETS_PLURAL, DIAGNOSTICS, "True", "Valid")
    cluster.fake.put(POLICY_BINDINGS_PLURAL, binding())
    await cluster.wait_for_natives(GRANTED)
    await cluster.wait_for_condition(POLICY_BINDINGS_PLURAL, GRANT, "True", "Applied")
    for native in cluster.natives():
        assert [(subject["kind"], subject["namespace"], subject["name"]) for subject in native["subjects"]] == [
            ("ServiceAccount", SUBJECT_NAMESPACE, ACCOUNT)
        ]
        assert native["metadata"]["labels"][POLICY_NAMESPACE_LABEL] == POLICY_NAMESPACE
        assert MANAGED_BY_LABEL in native["metadata"]["labels"]

    cluster.fake.delete(POLICY_BINDINGS_PLURAL, GRANT)
    await cluster.wait_for_natives(set())


async def test_native_binding_deleted_by_hand_is_recreated(cluster: Cluster) -> None:
    cluster.fake.put(POLICY_BINDINGS_PLURAL, binding())
    await cluster.wait_for_natives(GRANTED)
    cluster.fake.delete(CLUSTER_ROLE_BINDINGS_PLURAL, one(cluster.fake.objects[CLUSTER_ROLE_BINDINGS_PLURAL]))
    await cluster.wait_for_natives(GRANTED)


async def test_binding_grants_nothing_once_it_expires(cluster: Cluster) -> None:
    cluster.fake.put(POLICY_BINDINGS_PLURAL, binding(expires_at=datetime.now(UTC) + timedelta(seconds=2)))
    await cluster.wait_for_natives(GRANTED)
    # No event marks the deadline: only the controller's own timer can revoke the grant.
    await cluster.wait_for_natives(set())
    await cluster.wait_for_condition(POLICY_BINDINGS_PLURAL, GRANT, "False", "Expired")


@pytest.mark.parametrize("cluster_roles", [{NAMESPACE_READER}])
async def test_missing_role_is_reported_and_left_unbound(cluster: Cluster) -> None:
    cluster.fake.put(POLICY_BINDINGS_PLURAL, binding())
    condition = await cluster.wait_for_condition(POLICY_BINDINGS_PLURAL, GRANT, "False", "ApplyFailed")
    assert NODE_READER in condition["message"]
    assert cluster.native_roles() == GRANTED - {(None, NODE_READER)}


if __name__ == "__main__":
    pytest_bazel.main()
