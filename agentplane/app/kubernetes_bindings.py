"""Reconcile the concrete Kubernetes bindings of managed Sandbox ServiceAccounts."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import RbacAuthorizationV1Api

from agentplane.app.inventory import SandboxInventory, SandboxNotFoundError, SandboxView
from agentplane.app.kubernetes_grants import ResolvedGrant, RoleBindingGrant
from util.agent_sandbox import SANDBOX_API

logger = logging.getLogger(__name__)

MANAGED_BY_LABEL = "app.agentplane.allegedly.works/managed-by"
MANAGED_BY_APP = "integration-app"
SANDBOX_UID_ANNOTATION = "app.agentplane.allegedly.works/sandbox-uid"
SANDBOX_NAME_ANNOTATION = "app.agentplane.allegedly.works/sandbox-name"
KUBERNETES_BINDINGS_FINALIZER = "app.agentplane.allegedly.works/kubernetes-bindings"


class BindingConflictError(Exception):
    """A deterministic name belongs to a different binding; never adopt or remove it."""


def binding_name(sandbox: SandboxView, grant: ResolvedGrant) -> str:
    digest = hashlib.sha256(f"{sandbox.uid}/{grant.name}".encode()).hexdigest()[:16]
    return f"ap-{sandbox.name[:40]}-{digest}"


def _binding(sandbox: SandboxView, grant: ResolvedGrant) -> k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding:
    template = grant.grant
    owner_references = (
        [
            k8s_client.V1OwnerReference(
                api_version=SANDBOX_API.api_version,
                kind="Sandbox",
                name=sandbox.name,
                uid=str(sandbox.uid),
                controller=False,
                block_owner_deletion=False,
            )
        ]
        if isinstance(template, RoleBindingGrant) and template.namespace == sandbox.service_account.namespace
        else None
    )
    metadata = k8s_client.V1ObjectMeta(
        name=binding_name(sandbox, grant),
        namespace=template.namespace if isinstance(template, RoleBindingGrant) else None,
        labels={MANAGED_BY_LABEL: MANAGED_BY_APP},
        annotations={SANDBOX_UID_ANNOTATION: str(sandbox.uid), SANDBOX_NAME_ANNOTATION: sandbox.name},
        owner_references=owner_references,
    )
    role_ref = k8s_client.V1RoleRef(
        api_group="rbac.authorization.k8s.io", kind=template.role_ref.kind, name=template.role_ref.name
    )
    subjects = [
        k8s_client.RbacV1Subject(
            kind="ServiceAccount", name=sandbox.service_account.name, namespace=sandbox.service_account.namespace
        )
    ]
    if isinstance(template, RoleBindingGrant):
        return k8s_client.V1RoleBinding(metadata=metadata, role_ref=role_ref, subjects=subjects)
    return k8s_client.V1ClusterRoleBinding(metadata=metadata, role_ref=role_ref, subjects=subjects)


def _matches(
    actual: k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding,
    expected: k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding,
) -> bool:
    metadata, wanted = actual.metadata, expected.metadata
    return bool(
        type(actual) is type(expected)
        and metadata
        and wanted
        and metadata.labels
        and metadata.labels.get(MANAGED_BY_LABEL) == MANAGED_BY_APP
        and metadata.annotations
        and metadata.annotations.get(SANDBOX_UID_ANNOTATION) == wanted.annotations[SANDBOX_UID_ANNOTATION]
        and actual.role_ref == expected.role_ref
        and actual.subjects == expected.subjects
        and (metadata.owner_references or []) == (wanted.owner_references or [])
    )


class KubernetesBindings:
    """Retryable writer. The Sandbox annotation is the immutable desired assignment."""

    def __init__(
        self,
        inventory: SandboxInventory,
        rbac: RbacAuthorizationV1Api,
        *,
        cleanup_namespaces: set[str] | None = None,
        cleanup_cluster_bindings: bool = False,
    ) -> None:
        self._inventory = inventory
        self._rbac = rbac
        self._cleanup_namespaces = cleanup_namespaces or set()
        self._cleanup_cluster_bindings = cleanup_cluster_bindings

    async def ensure(self, sandbox: SandboxView) -> None:
        if sandbox.deleting or not sandbox.kubernetes_grants:
            return
        try:
            for grant in sandbox.kubernetes_grants:
                await self._ensure_one(sandbox, grant)
        except Exception as error:
            await self._report_error(sandbox, error)
        else:
            if not sandbox.kubernetes_grants_ready or sandbox.kubernetes_grant_error is not None:
                await self._inventory.set_kubernetes_grants_status(sandbox.name, ready=True)

    async def _report_error(self, sandbox: SandboxView, error: Exception) -> None:
        detail = f"{type(error).__name__}" + (
            f" ({error.status})" if isinstance(error, k8s_client.ApiException) else ""
        )
        logger.error("Kubernetes grant reconciliation failed for %s", sandbox.name, exc_info=error)
        await self._inventory.set_kubernetes_grants_status(sandbox.name, ready=False, error=detail)

    async def _read(
        self, expected: k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding
    ) -> k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding:
        assert expected.metadata is not None
        assert expected.metadata.name is not None
        if isinstance(expected, k8s_client.V1RoleBinding):
            assert expected.metadata.namespace is not None
            return await self._rbac.read_namespaced_role_binding(expected.metadata.name, expected.metadata.namespace)
        return await self._rbac.read_cluster_role_binding(expected.metadata.name)

    async def _create(self, expected: k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding) -> None:
        assert expected.metadata is not None
        if isinstance(expected, k8s_client.V1RoleBinding):
            assert expected.metadata.namespace is not None
            await self._rbac.create_namespaced_role_binding(expected.metadata.namespace, expected)
        else:
            await self._rbac.create_cluster_role_binding(expected)

    async def _delete(self, expected: k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding) -> None:
        assert expected.metadata is not None
        assert expected.metadata.name is not None
        if isinstance(expected, k8s_client.V1RoleBinding):
            assert expected.metadata.namespace is not None
            await self._rbac.delete_namespaced_role_binding(expected.metadata.name, expected.metadata.namespace)
        else:
            await self._rbac.delete_cluster_role_binding(expected.metadata.name)

    async def _ensure_one(self, sandbox: SandboxView, grant: ResolvedGrant) -> None:
        expected = _binding(sandbox, grant)
        # Kubernetes accepts a binding whose roleRef does not exist. Check the
        # referenced Role on every pass so a missing grant cannot become Ready.
        if expected.role_ref.kind == "Role":
            assert isinstance(expected, k8s_client.V1RoleBinding)
            assert expected.metadata is not None
            assert expected.metadata.namespace is not None
            await self._rbac.read_namespaced_role(expected.role_ref.name, expected.metadata.namespace)
        else:
            await self._rbac.read_cluster_role(expected.role_ref.name)
        try:
            actual = await self._read(expected)
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
            if sandbox.kubernetes_grants_ready:
                await self._inventory.set_kubernetes_grants_status(sandbox.name, ready=False)
            try:
                await self._create(expected)
            except k8s_client.ApiException as create_error:
                if create_error.status != 409:
                    raise
                actual = await self._read(expected)
            else:
                return
        if not _matches(actual, expected):
            raise BindingConflictError(
                f"binding {binding_name(sandbox, grant)} does not belong to Sandbox {sandbox.name}"
            )

    async def cleanup(self, sandbox: SandboxView) -> None:
        """Remove external bindings before allowing Kubernetes to delete the Sandbox."""
        if not sandbox.deleting:
            return
        try:
            for grant in sandbox.kubernetes_grants:
                template = grant.grant
                if isinstance(template, RoleBindingGrant) and template.namespace == sandbox.service_account.namespace:
                    continue  # Kubernetes garbage collection owns these.
                expected = _binding(sandbox, grant)
                try:
                    actual = await self._read(expected)
                except k8s_client.ApiException as error:
                    if error.status == 404:
                        continue
                    raise
                if not _matches(actual, expected):
                    raise BindingConflictError(
                        f"binding {binding_name(sandbox, grant)} does not belong to Sandbox {sandbox.name}"
                    )
                try:
                    await self._delete(expected)
                except k8s_client.ApiException as error:
                    if error.status != 404:
                        raise
        except Exception as error:
            await self._report_error(sandbox, error)
        else:
            await self._inventory.remove_finalizer(sandbox.name, KUBERNETES_BINDINGS_FINALIZER)

    async def reconcile_once(self) -> None:
        sandboxes = await self._inventory.list_sandboxes()
        for sandbox in sandboxes:
            if sandbox.deleting:
                await self.cleanup(sandbox)
            else:
                await self.ensure(sandbox)
        # A creation already in flight on another replica can land after finalizer
        # release. Sweep only bindings bearing our exact UID/name/SA stamp, so the
        # next pass also recovers crashes in that narrow race window.
        await self._sweep_orphans({str(sandbox.uid) for sandbox in sandboxes})

    async def _sweep_orphans(self, live_uids: set[str]) -> None:
        selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_APP}"
        for namespace in sorted(self._cleanup_namespaces):
            try:
                bindings = await self._rbac.list_namespaced_role_binding(namespace, label_selector=selector)
            except k8s_client.ApiException as error:
                if error.status == 404:
                    continue  # The namespace and its RoleBindings are already gone.
                raise
            for binding in bindings.items:
                if await self._is_confirmed_orphan(binding, live_uids):
                    assert binding.metadata is not None
                    assert binding.metadata.name is not None
                    try:
                        await self._rbac.delete_namespaced_role_binding(binding.metadata.name, namespace)
                    except k8s_client.ApiException as error:
                        if error.status != 404:
                            raise
        if self._cleanup_cluster_bindings:
            cluster_bindings = await self._rbac.list_cluster_role_binding(label_selector=selector)
            for cluster_binding in cluster_bindings.items:
                if await self._is_confirmed_orphan(cluster_binding, live_uids):
                    assert cluster_binding.metadata is not None
                    assert cluster_binding.metadata.name is not None
                    try:
                        await self._rbac.delete_cluster_role_binding(cluster_binding.metadata.name)
                    except k8s_client.ApiException as error:
                        if error.status != 404:
                            raise

    def _is_orphan(
        self, binding: k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding, live_uids: set[str]
    ) -> bool:
        metadata = binding.metadata
        if not metadata or not metadata.annotations or not metadata.labels:
            return False
        uid = metadata.annotations.get(SANDBOX_UID_ANNOTATION)
        name = metadata.annotations.get(SANDBOX_NAME_ANNOTATION)
        return bool(
            uid
            and uid not in live_uids
            and name
            and metadata.name
            and metadata.name.startswith(f"ap-{name[:40]}-")
            and not metadata.owner_references
            and metadata.labels.get(MANAGED_BY_LABEL) == MANAGED_BY_APP
            and binding.subjects
            and len(binding.subjects) == 1
            and binding.subjects[0].kind == "ServiceAccount"
            and binding.subjects[0].namespace == self._inventory.namespace
            and binding.subjects[0].name == name
        )

    async def _is_confirmed_orphan(
        self, binding: k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding, live_uids: set[str]
    ) -> bool:
        if not self._is_orphan(binding, live_uids):
            return False
        assert binding.metadata is not None
        assert binding.metadata.annotations is not None
        name = binding.metadata.annotations[SANDBOX_NAME_ANNOTATION]
        uid = binding.metadata.annotations[SANDBOX_UID_ANNOTATION]
        # A new Sandbox and binding can appear after the list snapshot. A direct
        # GET after seeing the binding avoids deleting its live grant as an orphan.
        try:
            current = await self._inventory.get(name)
        except SandboxNotFoundError:
            return True
        return str(current.uid) != uid

    async def run(self, *, interval_seconds: float = 30) -> None:
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Kubernetes grants reconciliation failed")
            await asyncio.sleep(interval_seconds)
