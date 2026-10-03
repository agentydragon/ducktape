"""Concrete launch choices stay attached to the Sandbox SA across retries and catalog edits."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict, ParseDict
from kubernetes_asyncio import client as k8s_client
from pydantic import ValidationError

from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import (
    KUBERNETES_BINDINGS_FINALIZER,
    KubernetesBindings,
    _binding,
    binding_name,
)
from agentplane.sandbox_service.kubernetes_grants import (
    ClusterRoleBindingGrant,
    ClusterRoleRef,
    DuplicateKubernetesGrantError,
    KubernetesGrant,
    RoleBindingGrant,
    RoleRef,
    UnknownKubernetesGrantError,
    resolve_grants,
)
from agentplane.sandbox_service.kubernetes_views import KUBERNETES_GRANTS_ANNOTATION, sandbox_view
from agentplane.sandbox_service.protocol_pb2 import CreateSandboxRequest, ResolvedGrant
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    TEMPLATE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    pod,
    sandbox,
)
from agentplane.sandbox_service.testing.fake_rbac import FakeRbac


def _grant(name: str) -> RoleBindingGrant:
    return RoleBindingGrant(kind="RoleBinding", namespace=NAMESPACE, role_ref=RoleRef(kind="Role", name=name))


async def _sandbox(
    inventory: SandboxInventory, core: FakeCoreV1Api, names: list[str], catalog: Mapping[str, KubernetesGrant]
) -> tuple[str, list[ResolvedGrant]]:
    selected = resolve_grants(names, catalog)
    view = await inventory.create(
        CreateSandboxRequest(slug="haku", template=TEMPLATE, kubernetes_grants=names),
        annotations={
            KUBERNETES_GRANTS_ANNOTATION: json.dumps(
                [MessageToDict(grant, preserving_proto_field_name=True) for grant in selected]
            )
        },
        finalizers=[KUBERNETES_BINDINGS_FINALIZER]
        if any(grant.grant.kind == "ClusterRoleBinding" or grant.grant.namespace != NAMESPACE for grant in selected)
        else None,
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
    assert not (await inventory.get(first)).kubernetes_grants_ready
    await bindings.reconcile_once()
    first_view, second_view = await inventory.get(first), await inventory.get(second)
    assert first_view.kubernetes_grants_ready
    assert second_view.kubernetes_grants_ready
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


async def test_missing_role_ref_never_reports_grants_ready() -> None:
    custom, core, rbac = FakeCustomObjectsApi(), FakeCoreV1Api(), FakeRbac()
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom), core_v1=cast(Any, core))
    catalog: dict[str, KubernetesGrant] = {
        "local": _grant("config-reader"),
        "cluster": ClusterRoleBindingGrant(
            kind="ClusterRoleBinding", role_ref=ClusterRoleRef(kind="ClusterRole", name="diagnostics")
        ),
    }
    name, _ = await _sandbox(inventory, core, ["local", "cluster"], catalog)
    bindings = KubernetesBindings(inventory, cast(Any, rbac))
    rbac.missing_roles.add((NAMESPACE, "config-reader"))
    await bindings.ensure(await inventory.get(name))
    assert not (await inventory.get(name)).kubernetes_grants_ready
    assert (await inventory.get(name)).kubernetes_grant_error == "ApiException (404)"
    assert rbac.creates == 0

    rbac.missing_roles.clear()
    await bindings.ensure(await inventory.get(name))
    assert (await inventory.get(name)).kubernetes_grants_ready
    assert rbac.creates == 2

    rbac.missing_cluster_roles.add("diagnostics")
    await bindings.ensure(await inventory.get(name))
    assert not (await inventory.get(name)).kubernetes_grants_ready
    assert (await inventory.get(name)).kubernetes_grant_error == "ApiException (404)"
    assert rbac.creates == 2

    rbac.missing_cluster_roles.clear()
    await bindings.ensure(await inventory.get(name))
    assert (await inventory.get(name)).kubernetes_grants_ready


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
    assert not failed.kubernetes_grants_ready
    assert failed.kubernetes_grant_error == "ApiException (503)"
    assert len(rbac.bindings) == 1
    # A restart with a changed catalog reads the Sandbox annotation; it does not resolve the
    # names against today's catalog or rewrite an existing binding's roleRef.
    await KubernetesBindings(inventory, cast(Any, rbac)).reconcile_once()
    recovered = await inventory.get(name)
    assert recovered.kubernetes_grants == selected
    assert recovered.kubernetes_grants_ready
    assert not recovered.HasField("kubernetes_grant_error")
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
    assert not (await inventory.get(name)).kubernetes_grants_ready
    assert (await inventory.get(name)).kubernetes_grant_error == "BindingConflictError"
    assert rbac.bindings[(NAMESPACE, binding.metadata.name)] is binding


async def test_external_scopes_recover_and_clean_up_after_deletion() -> None:
    custom, core, rbac = FakeCustomObjectsApi(), FakeCoreV1Api(), FakeRbac()
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom), core_v1=cast(Any, core))
    catalog: dict[str, KubernetesGrant] = {
        "logs": RoleBindingGrant(
            kind="RoleBinding",
            namespace="another-namespace",
            role_ref=RoleRef(kind="ClusterRole", name="logs-configmaps-reader"),
        ),
        "cluster": ClusterRoleBindingGrant(
            kind="ClusterRoleBinding", role_ref=ClusterRoleRef(kind="ClusterRole", name="cluster-diagnostics-reader")
        ),
    }
    name, selected = await _sandbox(inventory, core, ["logs", "cluster"], catalog)
    bindings = KubernetesBindings(
        inventory, cast(Any, rbac), cleanup_namespaces={"another-namespace"}, cleanup_cluster_bindings=True
    )
    rbac.fail_on_create = 2
    await bindings.reconcile_once()
    assert not (await inventory.get(name)).kubernetes_grants_ready
    await bindings.reconcile_once()
    view = await inventory.get(name)
    assert view.kubernetes_grants_ready
    cross = rbac.bindings[("another-namespace", binding_name(view, selected[0]))]
    cluster = rbac.cluster_bindings[binding_name(view, selected[1])]
    assert cross.metadata.owner_references is None
    assert cluster.metadata.owner_references is None
    assert [(subject.kind, subject.namespace, subject.name) for subject in cross.subjects] == [
        ("ServiceAccount", NAMESPACE, name)
    ]
    assert [(subject.kind, subject.namespace, subject.name) for subject in cluster.subjects] == [
        ("ServiceAccount", NAMESPACE, name)
    ]
    # A changed/removed catalog cannot retarget the persisted bindings, including after restart.
    await bindings.reconcile_once()
    assert rbac.creates == 3
    assert cross.role_ref.name == "logs-configmaps-reader"
    await inventory.suspend(name)
    await inventory.delete(name)
    assert (await inventory.get(name)).deleting
    rbac.fail_on_delete = 2
    await bindings.reconcile_once()
    assert (await inventory.get(name)).kubernetes_grant_error == "ApiException (503)"
    assert len(rbac.bindings) + len(rbac.cluster_bindings) == 1
    # A new reconciler resumes cleanup of only the remaining binding and releases the finalizer.
    await KubernetesBindings(
        inventory, cast(Any, rbac), cleanup_namespaces={"another-namespace"}, cleanup_cluster_bindings=True
    ).reconcile_once()
    assert not rbac.bindings
    assert not rbac.cluster_bindings
    assert ("sandboxes", name) not in custom.objects


async def test_external_name_collision_is_not_deleted() -> None:
    custom, core, rbac = FakeCustomObjectsApi(), FakeCoreV1Api(), FakeRbac()
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom), core_v1=cast(Any, core))
    catalog: dict[str, KubernetesGrant] = {
        "cluster": ClusterRoleBindingGrant(
            kind="ClusterRoleBinding", role_ref=ClusterRoleRef(kind="ClusterRole", name="cluster-diagnostics-reader")
        )
    }
    name, selected = await _sandbox(inventory, core, ["cluster"], catalog)
    view = await inventory.get(name)
    binding = k8s_client.V1ClusterRoleBinding(
        metadata=k8s_client.V1ObjectMeta(name=binding_name(view, selected[0])),
        role_ref=k8s_client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="other"),
        subjects=[k8s_client.RbacV1Subject(kind="ServiceAccount", namespace=NAMESPACE, name="someone-else")],
    )
    rbac.cluster_bindings[binding_name(view, selected[0])] = binding
    await inventory.suspend(name)
    await inventory.delete(name)
    await KubernetesBindings(inventory, cast(Any, rbac)).reconcile_once()
    assert rbac.cluster_bindings[binding_name(view, selected[0])] is binding
    assert (await inventory.get(name)).kubernetes_grant_error == "BindingConflictError"


async def test_partial_launch_deletion_and_name_reuse_leave_no_old_binding() -> None:
    custom, core, rbac = FakeCustomObjectsApi(), FakeCoreV1Api(), FakeRbac()
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom), core_v1=cast(Any, core))
    catalog: dict[str, KubernetesGrant] = {
        "cross": RoleBindingGrant(
            kind="RoleBinding", namespace="another-namespace", role_ref=RoleRef(kind="Role", name="reader")
        ),
        "cluster": ClusterRoleBindingGrant(
            kind="ClusterRoleBinding", role_ref=ClusterRoleRef(kind="ClusterRole", name="diagnostics")
        ),
    }
    name, selected = await _sandbox(inventory, core, ["cross", "cluster"], catalog)
    bindings = KubernetesBindings(
        inventory, cast(Any, rbac), cleanup_namespaces={"another-namespace"}, cleanup_cluster_bindings=True
    )
    rbac.fail_on_create = 2
    await bindings.reconcile_once()
    old_view = await inventory.get(name)
    old_binding = rbac.bindings[("another-namespace", binding_name(old_view, selected[0]))]
    await inventory.suspend(name)
    await inventory.delete(name)
    await bindings.reconcile_once()
    assert ("sandboxes", name) not in custom.objects
    assert not rbac.bindings
    # Model an in-flight writer landing after finalizer release. A new Sandbox may use
    # the same name but gets a distinct UID; the sweep must remove only the old grant.
    rbac.bindings[("another-namespace", binding_name(old_view, selected[0]))] = old_binding
    replacement = sandbox(name)
    replacement["metadata"]["annotations"] = {
        KUBERNETES_GRANTS_ANNOTATION: json.dumps([MessageToDict(selected[0], preserving_proto_field_name=True)])
    }
    custom.objects[("sandboxes", name)] = replacement
    await bindings.reconcile_once()
    new_view = await inventory.get(name)
    assert new_view.uid != old_view.uid
    assert ("another-namespace", binding_name(old_view, selected[0])) not in rbac.bindings
    assert ("another-namespace", binding_name(new_view, selected[0])) in rbac.bindings


async def test_orphan_sweep_checks_sandbox_created_after_list_snapshot() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom), core_v1=cast(Any, core))
    selected = ResolvedGrant(
        name="cluster",
        grant=ParseDict(
            ClusterRoleBindingGrant(
                kind="ClusterRoleBinding", role_ref=ClusterRoleRef(kind="ClusterRole", name="diagnostics")
            ).model_dump(mode="json"),
            protocol_pb2.KubernetesGrant(),
        ),
    )

    class LateRbac(FakeRbac):
        async def list_cluster_role_binding(self, *, label_selector: str) -> k8s_client.V1ClusterRoleBindingList:
            if not self.cluster_bindings:
                raw = sandbox("late")
                raw["metadata"]["annotations"] = {
                    KUBERNETES_GRANTS_ANNOTATION: json.dumps(
                        [MessageToDict(selected, preserving_proto_field_name=True)]
                    )
                }
                custom.objects[("sandboxes", "late")] = raw
                binding = _binding(sandbox_view(raw, None, api_client=core.api_client), selected)
                assert isinstance(binding, k8s_client.V1ClusterRoleBinding)
                assert binding.metadata is not None
                assert binding.metadata.name is not None
                self.cluster_bindings[binding.metadata.name] = binding
            return await super().list_cluster_role_binding(label_selector=label_selector)

    rbac = LateRbac()
    await KubernetesBindings(inventory, cast(Any, rbac), cleanup_cluster_bindings=True).reconcile_once()
    assert len(rbac.cluster_bindings) == 1
    assert rbac.deletes == 0


@pytest.mark.parametrize("status", [404, 403])
async def test_orphan_sweep_skips_only_missing_namespaces(status: int) -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom), core_v1=cast(Any, core))

    class MissingNamespaceRbac(FakeRbac):
        def __init__(self) -> None:
            super().__init__()
            self.listed: list[str] = []

        async def list_namespaced_role_binding(
            self, namespace: str, *, label_selector: str
        ) -> k8s_client.V1RoleBindingList:
            self.listed.append(namespace)
            if namespace == "a-missing":
                raise k8s_client.ApiException(status=status)
            return await super().list_namespaced_role_binding(namespace, label_selector=label_selector)

    rbac = MissingNamespaceRbac()
    bindings = KubernetesBindings(inventory, cast(Any, rbac), cleanup_namespaces={"a-missing", "z-present"})
    if status == 403:
        with pytest.raises(k8s_client.ApiException) as error:
            await bindings.reconcile_once()
        assert error.value.status == 403
        assert rbac.listed == ["a-missing"]
    else:
        await bindings.reconcile_once()
        assert rbac.listed == ["a-missing", "z-present"]


if __name__ == "__main__":
    pytest_bazel.main()
