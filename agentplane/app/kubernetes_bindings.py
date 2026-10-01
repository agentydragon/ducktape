"""Reconcile a managed Sandbox ServiceAccount's concrete Kubernetes grant bindings."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import RbacAuthorizationV1Api

from agentplane.app.inventory import SandboxInventory, SandboxView
from agentplane.app.kubernetes_grants import ResolvedGrant, RoleBindingGrant
from util.agent_sandbox import SANDBOX_API

logger = logging.getLogger(__name__)

MANAGED_BY_LABEL = "app.agentplane.allegedly.works/managed-by"
MANAGED_BY_APP = "integration-app"
SANDBOX_UID_ANNOTATION = "app.agentplane.allegedly.works/sandbox-uid"


class BindingConflictError(Exception):
    """A deterministic name already belongs to a different binding; never adopt it."""


def binding_name(sandbox: SandboxView, grant: ResolvedGrant) -> str:
    digest = hashlib.sha256(f"{sandbox.uid}/{grant.name}".encode()).hexdigest()[:16]
    return f"ap-{sandbox.name[:40]}-{digest}"


def _role_binding(sandbox: SandboxView, grant: ResolvedGrant) -> k8s_client.V1RoleBinding:
    template = grant.grant
    if not isinstance(template, RoleBindingGrant):
        raise ValueError(f"unsupported binding kind {template.kind}")
    if template.namespace != sandbox.service_account.namespace:
        raise ValueError(f"cross-namespace grant {grant.name!r} is not enabled yet")
    return k8s_client.V1RoleBinding(
        metadata=k8s_client.V1ObjectMeta(
            name=binding_name(sandbox, grant),
            namespace=template.namespace,
            labels={MANAGED_BY_LABEL: MANAGED_BY_APP},
            annotations={SANDBOX_UID_ANNOTATION: str(sandbox.uid)},
            owner_references=[
                k8s_client.V1OwnerReference(
                    api_version=SANDBOX_API.api_version,
                    kind="Sandbox",
                    name=sandbox.name,
                    uid=str(sandbox.uid),
                    controller=False,
                    block_owner_deletion=False,
                )
            ],
        ),
        role_ref=k8s_client.V1RoleRef(
            api_group="rbac.authorization.k8s.io", kind=template.role_ref.kind, name=template.role_ref.name
        ),
        subjects=[
            k8s_client.RbacV1Subject(
                kind="ServiceAccount", name=sandbox.service_account.name, namespace=sandbox.service_account.namespace
            )
        ],
    )


def _matches(actual: k8s_client.V1RoleBinding, expected: k8s_client.V1RoleBinding) -> bool:
    metadata, wanted = actual.metadata, expected.metadata
    return bool(
        metadata
        and wanted
        and metadata.labels
        and metadata.labels.get(MANAGED_BY_LABEL) == MANAGED_BY_APP
        and metadata.annotations
        and metadata.annotations.get(SANDBOX_UID_ANNOTATION) == wanted.annotations[SANDBOX_UID_ANNOTATION]
        and actual.role_ref == expected.role_ref
        and actual.subjects == expected.subjects
        and metadata.owner_references == wanted.owner_references
    )


class KubernetesBindings:
    """A retryable writer; the Sandbox annotation is the immutable desired assignment."""

    def __init__(self, inventory: SandboxInventory, rbac: RbacAuthorizationV1Api) -> None:
        self._inventory = inventory
        self._rbac = rbac

    async def ensure(self, sandbox: SandboxView) -> None:
        if not sandbox.kubernetes_grants:
            return
        try:
            for grant in sandbox.kubernetes_grants:
                await self._ensure_one(sandbox, grant)
        except Exception as error:
            detail = f"{type(error).__name__}" + (
                f" ({error.status})" if isinstance(error, k8s_client.ApiException) else ""
            )
            logger.exception("Kubernetes grant provisioning failed for %s", sandbox.name)
            await self._inventory.set_kubernetes_grants_status(sandbox.name, ready=False, error=detail)
        else:
            if not sandbox.kubernetes_grants_ready or sandbox.kubernetes_grant_error is not None:
                await self._inventory.set_kubernetes_grants_status(sandbox.name, ready=True)

    async def _ensure_one(self, sandbox: SandboxView, grant: ResolvedGrant) -> None:
        expected = _role_binding(sandbox, grant)
        assert expected.metadata is not None
        assert expected.metadata.namespace is not None
        assert expected.metadata.name is not None
        namespace, name = expected.metadata.namespace, expected.metadata.name
        try:
            actual = await self._rbac.read_namespaced_role_binding(name, namespace)
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
            if sandbox.kubernetes_grants_ready:
                await self._inventory.set_kubernetes_grants_status(sandbox.name, ready=False)
            try:
                await self._rbac.create_namespaced_role_binding(namespace, expected)
            except k8s_client.ApiException as create_error:
                if create_error.status != 409:
                    raise
                actual = await self._rbac.read_namespaced_role_binding(name, namespace)
            else:
                return
        if not _matches(actual, expected):
            raise BindingConflictError(f"binding {namespace}/{name} does not belong to Sandbox {sandbox.name}")

    async def reconcile_once(self) -> None:
        for sandbox in await self._inventory.list_sandboxes():
            await self.ensure(sandbox)

    async def run(self, *, interval_seconds: float = 30) -> None:
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Kubernetes grants reconciliation failed")
            await asyncio.sleep(interval_seconds)
