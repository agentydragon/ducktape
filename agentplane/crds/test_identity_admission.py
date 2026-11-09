"""Exercise the identity and Kubernetes account kinds' schemas, and the PolicyBinding subject that names an
identity, through Kubernetes admission and CEL."""

from typing import Any, cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
NAMESPACE = "identity-admission"
SERVICE_ACCOUNT = {"namespace": "test-accounts", "name": "test-shared-account"}


class Kinds:
    def __init__(self, api: k8s_client.CustomObjectsApi) -> None:
        self.api = api

    async def create(self, plural: str, kind: str, name: str, spec: dict[str, Any], **metadata: Any) -> dict[str, Any]:
        body = {"apiVersion": f"{GROUP}/{VERSION}", "kind": kind, "metadata": {"name": name, **metadata}, "spec": spec}
        return cast(
            dict[str, Any], await self.api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, body)
        )

    async def rejects_create(self, plural: str, kind: str, spec: dict[str, Any]) -> None:
        with pytest.raises(ApiException) as rejected:
            await self.create(plural, kind, "rejected", spec)
        assert rejected.value.status == 422, f"{spec}: {rejected.value.body!r}"

    async def replace_status(self, plural: str, obj: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.api.replace_namespaced_custom_object_status(
                GROUP, VERSION, NAMESPACE, plural, obj["metadata"]["name"], {**obj, "status": status}
            ),
        )

    async def rejects_status(self, plural: str, obj: dict[str, Any], status: dict[str, Any]) -> None:
        with pytest.raises(ApiException) as rejected:
            await self.replace_status(plural, obj, status)
        assert rejected.value.status == 422, f"{status}: {rejected.value.body!r}"


async def _identity(kinds: Kinds) -> dict[str, Any]:
    identity = await kinds.create("identities", "Identity", "test-agent", {"description": "Test agent"})
    for spec in ({}, {"description": ""}):
        await kinds.rejects_create("identities", "Identity", spec)
    return identity


async def _role_sets(kinds: Kinds) -> None:
    role_binding = {"type": "role_binding", "role": {"kind": "ClusterRole", "name": "test-reader"}, "namespaces": ["a"]}
    cluster_role_binding = {"type": "cluster_role_binding", "clusterRole": "test-node-reader"}
    await kinds.create(
        "kubernetesrolesets",
        "KubernetesRoleSet",
        "test-roles",
        {
            "rules": [
                role_binding,
                {**role_binding, "role": {"kind": "Role", "name": "test-reader"}},
                cluster_role_binding,
            ]
        },
    )
    for rule in (
        # PolicySet's spelling of the same rule is not a KubernetesRoleSet rule type.
        {**role_binding, "type": "kubernetes_role_binding"},
        {"type": "egress", "hosts": ["api.example.test"]},
        {**role_binding, "role": {"kind": "Group", "name": "test-reader"}},
        {"type": "role_binding", "role": role_binding["role"]},
        {"type": "role_binding", "namespaces": ["a"]},
        {**role_binding, "clusterRole": "test-node-reader"},
        {**cluster_role_binding, "namespaces": ["a"]},
        {"type": "cluster_role_binding"},
    ):
        await kinds.rejects_create("kubernetesrolesets", "KubernetesRoleSet", {"rules": [rule]})
    await kinds.rejects_create("kubernetesrolesets", "KubernetesRoleSet", {"rules": []})


async def _grants(kinds: Kinds) -> None:
    await kinds.create(
        "serviceaccountgrants",
        "ServiceAccountGrant",
        "test-standing",
        {"serviceAccount": SERVICE_ACCOUNT, "roleSets": ["test-roles"]},
    )
    grant = await kinds.create(
        "serviceaccountgrants",
        "ServiceAccountGrant",
        "test-expiring",
        {"serviceAccount": SERVICE_ACCOUNT, "roleSets": ["test-more-roles"], "expiresAt": "2026-11-05T13:00:00Z"},
    )
    condition = {
        "type": "KubernetesReady",
        "status": "False",
        "reason": "RoleMissing",
        "message": "test role missing",
        "lastTransitionTime": "2026-11-05T12:00:00Z",
    }
    missing = {"kind": "ClusterRole", "name": "test-reader"}
    grant = await kinds.replace_status(
        "serviceaccountgrants", grant, {"conditions": [condition], "missingRoles": [missing]}
    )
    assert grant["status"]["missingRoles"] == [missing]

    for spec in (
        {"serviceAccount": SERVICE_ACCOUNT, "roleSets": []},
        {"serviceAccount": SERVICE_ACCOUNT},
        {"serviceAccount": SERVICE_ACCOUNT, "roleSets": [""]},
        {"serviceAccount": {k: v for k, v in SERVICE_ACCOUNT.items() if k != "namespace"}, "roleSets": ["test-roles"]},
        {"roleSets": ["test-roles"]},
    ):
        await kinds.rejects_create("serviceaccountgrants", "ServiceAccountGrant", spec)


async def _managed_service_accounts(kinds: Kinds, identity: dict[str, Any]) -> None:
    owner = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Identity",
        "name": identity["metadata"]["name"],
        "uid": identity["metadata"]["uid"],
    }
    account = await kinds.create(
        "managedserviceaccounts",
        "ManagedServiceAccount",
        "test-agent-extra",
        {"expiresAt": "2026-11-05T12:34:00Z"},
        ownerReferences=[owner],
    )
    created = {"namespace": "test-accounts", "name": "test-agent-extra", "uid": "3b1c0f1e-7a52-4f0c-9d52-2d7a54f0b2a1"}
    collision = {
        "type": "Ready",
        "status": "False",
        "reason": "NameConflict",
        "message": "test foreign ServiceAccount",
        "lastTransitionTime": "2026-11-05T12:00:00Z",
    }
    account = await kinds.replace_status(
        "managedserviceaccounts", account, {"serviceAccount": created, "conditions": [collision]}
    )
    assert account["status"] == {"serviceAccount": created, "conditions": [collision]}

    await kinds.rejects_status("managedserviceaccounts", account, {"serviceAccount": {**created, "uid": "not-a-uid"}})
    # An account with no expiry stands until its owner goes.
    await kinds.create("managedserviceaccounts", "ManagedServiceAccount", "test-standing-extra", {})


async def _binding_subjects(kinds: Kinds) -> None:
    service_account: dict[str, Any] = {"namespace": NAMESPACE, "name": "test-workload"}
    identity: dict[str, Any] = {"identity": {"name": "test-agent"}}
    for name, subject in (("test-service-account", service_account), ("test-identity", identity)):
        created = await kinds.create(
            "policybindings", "PolicyBinding", name, {"subject": subject, "policySets": ["basic"]}
        )
        assert created["spec"]["subject"] == subject

    for subject in (
        {**service_account, **identity},
        {"namespace": NAMESPACE, **identity},
        {},
        {"namespace": NAMESPACE},
        {"name": "test-workload"},
        {"identity": {"name": ""}},
    ):
        await kinds.rejects_create("policybindings", "PolicyBinding", {"subject": subject, "policySets": ["basic"]})


async def test_identity_and_kubernetes_account_admission() -> None:
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
        kinds = Kinds(k8s_client.CustomObjectsApi(server.api))
        identity = await _identity(kinds)
        await _role_sets(kinds)
        await _grants(kinds)
        await _managed_service_accounts(kinds, identity)
        await _binding_subjects(kinds)


if __name__ == "__main__":
    pytest_bazel.main()
