"""Concrete launch choices stay attached to the Sandbox SA across retries and catalog edits."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from pydantic import ValidationError

from agentplane.app.inventory import KUBERNETES_GRANTS_ANNOTATION, NewSandbox, ProvisioningState, SandboxInventory
from agentplane.app.kubernetes_bindings import KubernetesBindings, binding_name
from agentplane.app.kubernetes_grants import (
    ClusterRoleBindingGrant,
    DuplicateKubernetesGrantError,
    ResolvedGrant,
    RoleBindingGrant,
    RoleRef,
    UnknownKubernetesGrantError,
    resolve_grants,
)
from agentplane.app.testing.kubernetes import NAMESPACE, TEMPLATE, FakeCoreV1Api, FakeCustomObjectsApi, pod


class FakeRbac:
    def __init__(self) -> None:
        self.bindings: dict[tuple[str, str], k8s_client.V1RoleBinding] = {}
        self.creates = 0
        self.fail_on_create: int | None = None

    async def read_namespaced_role_binding(self, name: str, namespace: str) -> k8s_client.V1RoleBinding:
        try:
            return self.bindings[(namespace, name)]
        except KeyError:
            raise k8s_client.ApiException(status=404) from None

    async def create_namespaced_role_binding(
        self, namespace: str, body: k8s_client.V1RoleBinding
    ) -> k8s_client.V1RoleBinding:
        self.creates += 1
        if self.creates == self.fail_on_create:
            raise k8s_client.ApiException(status=503)
        assert body.metadata is not None
        assert body.metadata.name is not None
        key = (namespace, body.metadata.name)
        if key in self.bindings:
            raise k8s_client.ApiException(status=409)
        self.bindings[key] = body
        return body


def _grant(name: str) -> RoleBindingGrant:
    return RoleBindingGrant(kind="RoleBinding", namespace=NAMESPACE, role_ref=RoleRef(kind="Role", name=name))


async def _sandbox(
    inventory: SandboxInventory, core: FakeCoreV1Api, names: list[str], catalog: dict[str, RoleBindingGrant]
) -> tuple[str, list[ResolvedGrant]]:
    selected = resolve_grants(names, catalog)
    view = await inventory.create(
        NewSandbox(slug="haku", template=TEMPLATE, kubernetes_grants=names),
        annotations={KUBERNETES_GRANTS_ANNOTATION: json.dumps([grant.model_dump(mode="json") for grant in selected])},
    )
    core.pods[view.name] = pod(view.name, phase="Running", ready=True, ip="10.0.0.1")
    return view.name, selected


def test_catalog_rejects_unknown_and_invalid_scope() -> None:
    catalog = {"config": _grant("config-reader")}
    with pytest.raises(UnknownKubernetesGrantError):
        resolve_grants(["anything-else"], catalog)
    with pytest.raises(DuplicateKubernetesGrantError):
        resolve_grants(["config", "config"], catalog)
    with pytest.raises(ValidationError):
        ClusterRoleBindingGrant.model_validate(
            {"kind": "ClusterRoleBinding", "namespace": NAMESPACE, "role_ref": {"kind": "Role", "name": "config"}}
        )


async def test_distinct_sandboxes_bind_only_their_own_service_accounts() -> None:
    custom, core, rbac = FakeCustomObjectsApi(), FakeCoreV1Api(), FakeRbac()
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom), core_v1=cast(Any, core))
    catalog = {"config": _grant("config-reader")}
    first, selection = await _sandbox(inventory, core, ["config"], catalog)
    second, _ = await _sandbox(inventory, core, ["config"], catalog)
    bindings = KubernetesBindings(inventory, cast(Any, rbac))
    assert (await inventory.get(first)).state is ProvisioningState.WAITING_FOR_GRANTS
    await bindings.reconcile_once()
    first_view, second_view = await inventory.get(first), await inventory.get(second)
    assert first_view.state is second_view.state is ProvisioningState.RUNNING
    assert first_view.kubernetes_grants == selection
    assert len(rbac.bindings) == 2
    for view in (first_view, second_view):
        role_binding = rbac.bindings[(NAMESPACE, binding_name(view, selection[0]))]
        assert [(subject.kind, subject.namespace, subject.name) for subject in role_binding.subjects] == [
            ("ServiceAccount", NAMESPACE, view.name)
        ]
        assert role_binding.role_ref.name == "config-reader"
        assert role_binding.metadata.owner_references[0].uid == str(view.uid)
    await bindings.reconcile_once()
    assert rbac.creates == 2


async def test_partial_creation_recovers_from_persisted_selection_after_catalog_removal() -> None:
    custom, core, rbac = FakeCustomObjectsApi(), FakeCoreV1Api(), FakeRbac()
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom), core_v1=cast(Any, core))
    name, selected = await _sandbox(
        inventory, core, ["config", "other"], {"config": _grant("config-reader"), "other": _grant("other-reader")}
    )
    rbac.fail_on_create = 2
    bindings = KubernetesBindings(inventory, cast(Any, rbac))
    await bindings.ensure(await inventory.get(name))
    failed = await inventory.get(name)
    assert failed.state is ProvisioningState.WAITING_FOR_GRANTS
    assert failed.kubernetes_grant_error == "ApiException (503)"
    assert len(rbac.bindings) == 1
    # A restart with a changed catalog reads the Sandbox annotation; it does not resolve the
    # names against today's catalog or rewrite an existing binding's roleRef.
    await KubernetesBindings(inventory, cast(Any, rbac)).reconcile_once()
    recovered = await inventory.get(name)
    assert recovered.kubernetes_grants == selected
    assert recovered.state is ProvisioningState.RUNNING
    assert recovered.kubernetes_grant_error is None
    assert len(rbac.bindings) == 2


async def test_same_name_with_another_subject_is_never_adopted() -> None:
    custom, core, rbac = FakeCustomObjectsApi(), FakeCoreV1Api(), FakeRbac()
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom), core_v1=cast(Any, core))
    name, selected = await _sandbox(inventory, core, ["config"], {"config": _grant("config-reader")})
    view = await inventory.get(name)
    binding = k8s_client.V1RoleBinding(
        metadata=k8s_client.V1ObjectMeta(name=binding_name(view, selected[0]), namespace=NAMESPACE),
        role_ref=k8s_client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name="config-reader"),
        subjects=[k8s_client.RbacV1Subject(kind="ServiceAccount", name="somebody-else", namespace=NAMESPACE)],
    )
    rbac.bindings[(NAMESPACE, binding.metadata.name)] = binding
    await KubernetesBindings(inventory, cast(Any, rbac)).ensure(view)
    assert (await inventory.get(name)).state is ProvisioningState.WAITING_FOR_GRANTS
    assert (await inventory.get(name)).kubernetes_grant_error == "BindingConflictError"
    assert rbac.bindings[(NAMESPACE, binding.metadata.name)] is binding


if __name__ == "__main__":
    pytest_bazel.main()
