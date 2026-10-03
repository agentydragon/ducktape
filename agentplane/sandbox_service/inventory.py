"""Agentplane's sandbox inventory: the labelled Sandboxes in one namespace and the Pod under each.

Kubernetes is the inventory in this slice — this component persists no private database — so every
fact it knows about a sandbox is a label or annotation on its Sandbox. It projects the Sandbox CR
status and its same-name Pod status separately, without deriving a combined provisioning state. It
creates standalone Sandboxes: the Pod and volume shape is copied from the namespace's
`SandboxTemplate` at creation, so the manifest stays the one place the runner Pod is defined, and no
claim or warm pool sits in between.
"""

from __future__ import annotations

import asyncio
import secrets
import string
from typing import cast

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api
from pydantic import BaseModel, ConfigDict, Field

from agentplane.action_service.policies.resources import CALLER_LABEL
from agentplane.sandbox_service.binding_storage import read_binding
from agentplane.sandbox_service.kubernetes_views import (
    KUBERNETES_GRANTS_ERROR_ANNOTATION,
    KUBERNETES_GRANTS_READY_ANNOTATION,
    MANAGED_LABEL,
    PROVISIONING_ANNOTATION,
    SANDBOX_BINDING_ANNOTATION,
    SandboxResource,
    sandbox_view,
    sandbox_views,
)
from agentplane.sandbox_service.models import OperatingMode, SandboxNotFoundError, SandboxRunningError
from agentplane.sandbox_service.protocol_pb2 import CreateSandboxRequest, Sandbox, SandboxBinding
from agentplane.sandbox_service.session_config import LaunchGrants
from util.agent_sandbox import EXTENSIONS_API, SANDBOX_API, SANDBOXES_PLURAL, TEMPLATES_PLURAL
from util.kubernetes import CustomObjectsClient

_MERGE_PATCH = "application/merge-patch+json"

# Five lowercase alphanumerics, like `generateName`; the slug bound keeps the name a DNS label.
_SUFFIX_LENGTH = 5
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits

# Kubernetes-boundary models: the subset of each CR the inventory reads, parsed once off the wire.


class _TemplateSpec(BaseModel):
    """The parts of a SandboxTemplate a standalone Sandbox carries verbatim."""

    model_config = ConfigDict(extra="ignore")

    pod_template: dict[str, object] = Field(alias="podTemplate")
    volume_claim_templates: list[dict[str, object]] = Field(alias="volumeClaimTemplates", default_factory=list)


class _Template(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spec: _TemplateSpec


class _NamedMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class _NamedResource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _NamedMetadata


class _ResourceList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[dict[str, object]]


class SandboxInventory:
    def __init__(self, *, namespace: str, custom_objects: CustomObjectsClient, core_v1: CoreV1Api):
        self._namespace = namespace
        self._custom_objects = custom_objects
        self._core_v1 = core_v1

    @property
    def namespace(self) -> str:
        return self._namespace

    async def list_templates(self) -> list[str]:
        """The concrete templates an operator may choose for one Sandbox."""
        page = await self._custom_objects.list_namespaced_custom_object(
            *EXTENSIONS_API, self._namespace, TEMPLATES_PLURAL
        )
        return sorted(
            _NamedResource.model_validate(item).metadata.name for item in _ResourceList.model_validate(page).items
        )

    async def list_sandboxes(self) -> list[Sandbox]:
        sandboxes_page, pods = await asyncio.gather(
            self._custom_objects.list_namespaced_custom_object(
                *SANDBOX_API, self._namespace, SANDBOXES_PLURAL, label_selector=f"{MANAGED_LABEL}=true"
            ),
            self._core_v1.list_namespaced_pod(self._namespace),
        )
        # kubernetes_asyncio exposes this runtime attribute but omits it from generated SDK stubs.
        api_client = self._core_v1.api_client  # type: ignore[attr-defined]
        return sandbox_views(_ResourceList.model_validate(sandboxes_page).items, pods.items, api_client=api_client)

    async def get(self, name: str) -> Sandbox:
        sandbox = await self._sandbox(name)
        # kubernetes_asyncio exposes this runtime attribute but omits it from generated SDK stubs.
        api_client = self._core_v1.api_client  # type: ignore[attr-defined]
        return sandbox_view(sandbox, await self._pod(name), api_client=api_client)

    async def create(
        self,
        spec: CreateSandboxRequest,
        *,
        annotations: dict[str, str] | None = None,
        finalizers: list[str] | None = None,
    ) -> Sandbox:
        template = _Template.model_validate(
            await self._custom_objects.get_namespaced_custom_object(
                *EXTENSIONS_API, self._namespace, TEMPLATES_PLURAL, spec.template
            )
        )
        suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))
        name = f"{spec.slug}-{suffix}"
        # Before the Sandbox, because its Pod names this ServiceAccount: a Pod whose ServiceAccount
        # does not exist is refused admission, and the token projected for the proxy's audience is
        # minted for it. The owner reference cannot be set yet -- the Sandbox has no UID until it is
        # created -- so it is patched on directly afterwards and the account is deleted if the
        # Sandbox never appears, rather than being left for nothing to collect.
        await self._core_v1.create_namespaced_service_account(
            self._namespace,
            k8s_client.V1ServiceAccount(
                metadata=k8s_client.V1ObjectMeta(
                    # The Action Service admits an account only while it carries its caller label,
                    # so a sandbox without this one authenticates and reaches no route.
                    name=name,
                    labels={MANAGED_LABEL: "true", CALLER_LABEL: "true"},
                )
            ),
        )
        body = {
            "apiVersion": SANDBOX_API.api_version,
            "kind": "Sandbox",
            "metadata": {
                "name": name,
                "labels": {MANAGED_LABEL: "true"},
                **({"annotations": annotations} if annotations else {}),
                **({"finalizers": finalizers} if finalizers else {}),
            },
            # No shutdownTime and Retain: the app owns deletion, nothing expires a sandbox behind it.
            "spec": {
                "podTemplate": _running_as(template.spec.pod_template, name),
                "volumeClaimTemplates": template.spec.volume_claim_templates,
                "shutdownPolicy": "Retain",
            },
        }
        try:
            created = await self._custom_objects.create_namespaced_custom_object(
                *SANDBOX_API, self._namespace, SANDBOXES_PLURAL, body
            )
        except Exception:
            await self._core_v1.delete_namespaced_service_account(name, self._namespace)
            raise
        sandbox = SandboxResource.model_validate(created)
        await self._core_v1.patch_namespaced_service_account(
            name,
            self._namespace,
            {
                "metadata": {
                    "ownerReferences": [
                        {
                            "apiVersion": SANDBOX_API.api_version,
                            "kind": "Sandbox",
                            "name": name,
                            "uid": str(sandbox.metadata.uid),
                            "controller": False,
                            "blockOwnerDeletion": False,
                        }
                    ]
                }
            },
        )
        # kubernetes_asyncio exposes this runtime attribute but omits it from generated SDK stubs.
        api_client = self._core_v1.api_client  # type: ignore[attr-defined]
        return sandbox_view(sandbox, None, api_client=api_client)

    async def pending_grants(self, name: str) -> LaunchGrants | None:
        raw = (await self._sandbox(name)).metadata.annotations.get(PROVISIONING_ANNOTATION)
        return LaunchGrants.model_validate_json(raw) if raw is not None else None

    async def finish_provisioning(self, sandbox: Sandbox) -> None:
        await self._patch(
            sandbox.name, {"metadata": {"uid": str(sandbox.uid), "annotations": {PROVISIONING_ANNOTATION: None}}}
        )

    async def binding(self, name: str) -> SandboxBinding | None:
        raw = (await self._sandbox(name)).metadata.annotations.get(SANDBOX_BINDING_ANNOTATION)
        if raw is None:
            return None
        return read_binding(raw)

    async def set_kubernetes_grants_status(self, name: str, *, ready: bool, error: str | None = None) -> None:
        """Record provisioning so a runner cannot start before requested bindings exist."""
        await self._patch(
            name,
            {
                "metadata": {
                    "annotations": {
                        KUBERNETES_GRANTS_READY_ANNOTATION: "true" if ready else "false",
                        KUBERNETES_GRANTS_ERROR_ANNOTATION: error,
                    }
                }
            },
        )

    async def remove_finalizer(self, name: str, finalizer: str) -> None:
        sandbox = await self._sandbox(name)
        if finalizer in sandbox.metadata.finalizers:
            await self._patch(
                name, {"metadata": {"finalizers": [item for item in sandbox.metadata.finalizers if item != finalizer]}}
            )

    async def suspend(self, name: str, *, uid: str | None = None) -> None:
        await self._set_operating_mode(name, OperatingMode.SUSPENDED, uid=uid)

    async def resume(self, name: str, *, uid: str | None = None) -> None:
        await self._set_operating_mode(name, OperatingMode.RUNNING, uid=uid)

    async def delete(self, name: str, *, uid: str | None = None) -> None:
        """Delete a suspended Sandbox; the controller removes its Pod and PVC, and with them
        everything on the volume. A running one is refused, so the irreversible step is a
        deliberate second one for a browser and for an agent calling the API alike."""
        sandbox = await self._sandbox(name)
        if uid is not None and sandbox.metadata.uid != uid:
            raise SandboxNotFoundError(name)
        if sandbox.spec.operating_mode != OperatingMode.SUSPENDED:
            raise SandboxRunningError(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *SANDBOX_API,
            self._namespace,
            SANDBOXES_PLURAL,
            name,
            body=k8s_client.V1DeleteOptions(preconditions=k8s_client.V1Preconditions(uid=str(sandbox.metadata.uid))),
        )

    async def _set_operating_mode(self, name: str, mode: OperatingMode, *, uid: str | None = None) -> None:
        sandbox = await self._sandbox(name)
        if uid is not None and sandbox.metadata.uid != uid:
            raise SandboxNotFoundError(name)
        await self._patch(name, {"metadata": {"uid": str(sandbox.metadata.uid)}, "spec": {"operatingMode": mode}})

    async def _patch(self, name: str, patch: dict[str, object]) -> None:
        await self._custom_objects.patch_namespaced_custom_object(
            *SANDBOX_API, self._namespace, SANDBOXES_PLURAL, name, patch, _content_type=_MERGE_PATCH
        )

    async def _sandbox(self, name: str) -> SandboxResource:
        """The named Sandbox, only if it is Agentplane's: an unmanaged one is not in this inventory."""
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                *SANDBOX_API, self._namespace, SANDBOXES_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise SandboxNotFoundError(name) from error
            raise
        sandbox = SandboxResource.model_validate(raw)
        if sandbox.metadata.labels.get(MANAGED_LABEL) != "true":
            raise SandboxNotFoundError(name)
        return sandbox

    async def _pod(self, name: str) -> k8s_client.V1Pod | None:
        try:
            return await self._core_v1.read_namespaced_pod(name, self._namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                return None
            raise


# The projection, over objects however they were obtained: one request's list, or the copy
# `live.py` keeps under a watch. Both go through here, so a pushed row and a fetched one are the
# same row.


def _running_as(pod_template: dict[str, object], service_account: str) -> dict[str, object]:
    """The template's Pod, running as this sandbox's own ServiceAccount rather than the shared one.

    What a Pod runs as is what the egress proxy and the Action Service authenticate it by, so an
    account of its own is what lets a sandbox be granted something its neighbours are not. The
    template still owns every other field, including whether a token is automounted.
    """
    spec = {**cast(dict[str, object], pod_template.get("spec", {})), "serviceAccountName": service_account}
    return {**pod_template, "spec": spec}


# gazelle:include_dep @pypi//protobuf
