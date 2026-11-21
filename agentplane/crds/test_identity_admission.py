"""Exercise the identity and Kubernetes account kinds' schemas, and the PolicyBinding subject that names an
identity, through Kubernetes admission and CEL."""

from typing import Any

import pytest_bazel

from agentplane.crd_group import GROUP, VERSION
from agentplane.testing.crd_admission import CrdAdmission, crd_namespace

NAMESPACE = "identity-admission"
SERVICE_ACCOUNT = {"namespace": "test-accounts", "name": "test-shared-account"}


async def _identity(identities: CrdAdmission) -> dict[str, Any]:
    identity = await identities.create("test-agent", {"description": "Test agent"})
    for spec in ({}, {"description": ""}):
        await identities.rejects_create(spec)
    return identity


async def _role_sets(role_sets: CrdAdmission) -> None:
    role_binding = {"type": "role_binding", "role": {"kind": "ClusterRole", "name": "test-reader"}, "namespaces": ["a"]}
    cluster_role_binding = {"type": "cluster_role_binding", "clusterRole": "test-node-reader"}
    await role_sets.create(
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
        await role_sets.rejects_create({"rules": [rule]})
    await role_sets.rejects_create({"rules": []})


async def _grants(grants: CrdAdmission) -> None:
    await grants.create("test-standing", {"serviceAccount": SERVICE_ACCOUNT, "roleSets": ["test-roles"]})
    grant = await grants.create(
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
    grant = await grants.replace_status(grant, {"conditions": [condition], "missingRoles": [missing]})
    assert grant["status"]["missingRoles"] == [missing]

    for spec in (
        {"serviceAccount": SERVICE_ACCOUNT, "roleSets": []},
        {"serviceAccount": SERVICE_ACCOUNT},
        {"serviceAccount": SERVICE_ACCOUNT, "roleSets": [""]},
        {"serviceAccount": {k: v for k, v in SERVICE_ACCOUNT.items() if k != "namespace"}, "roleSets": ["test-roles"]},
        {"roleSets": ["test-roles"]},
    ):
        await grants.rejects_create(spec)


async def _managed_service_accounts(accounts: CrdAdmission, identity: dict[str, Any]) -> None:
    owner = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Identity",
        "name": identity["metadata"]["name"],
        "uid": identity["metadata"]["uid"],
    }
    account = await accounts.create("test-agent-extra", {"expiresAt": "2026-11-05T12:34:00Z"}, ownerReferences=[owner])
    created = {"namespace": "test-accounts", "name": "test-agent-extra", "uid": "3b1c0f1e-7a52-4f0c-9d52-2d7a54f0b2a1"}
    collision = {
        "type": "Ready",
        "status": "False",
        "reason": "NameConflict",
        "message": "test foreign ServiceAccount",
        "lastTransitionTime": "2026-11-05T12:00:00Z",
    }
    account = await accounts.replace_status(account, {"serviceAccount": created, "conditions": [collision]})
    assert account["status"] == {"serviceAccount": created, "conditions": [collision]}

    await accounts.rejects_status(account, {"serviceAccount": {**created, "uid": "not-a-uid"}})
    # An account with no expiry stands until its owner goes.
    await accounts.create("test-standing-extra", {})


async def _binding_subjects(bindings: CrdAdmission) -> None:
    service_account: dict[str, Any] = {"namespace": NAMESPACE, "name": "test-workload"}
    identity: dict[str, Any] = {"identity": {"name": "test-agent"}}
    for name, subject in (("test-service-account", service_account), ("test-identity", identity)):
        created = await bindings.create(name, {"subject": subject, "policySets": ["basic"]})
        assert created["spec"]["subject"] == subject

    for subject in (
        {**service_account, **identity},
        {"namespace": NAMESPACE, **identity},
        {},
        {"namespace": NAMESPACE},
        {"name": "test-workload"},
        {"identity": {"name": ""}},
    ):
        await bindings.rejects_create({"subject": subject, "policySets": ["basic"]})


async def test_identity_and_kubernetes_account_admission() -> None:
    async with crd_namespace(NAMESPACE) as namespace:
        identity = await _identity(namespace.kind("identities", "Identity"))
        await _role_sets(namespace.kind("kubernetesrolesets", "KubernetesRoleSet"))
        await _grants(namespace.kind("serviceaccountgrants", "ServiceAccountGrant"))
        await _managed_service_accounts(namespace.kind("managedserviceaccounts", "ManagedServiceAccount"), identity)
        await _binding_subjects(namespace.kind("policybindings", "PolicyBinding"))


if __name__ == "__main__":
    pytest_bazel.main()
