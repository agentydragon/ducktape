"""Agentplane's sandbox inventory: the labelled Sandboxes in one namespace and the Pod under each.

Kubernetes is the inventory in this slice — the app persists nothing of its own — so every fact the
app knows about a sandbox is a label or annotation on its Sandbox, and the provisioning state is
derived from the Sandbox and its Pod. The app creates standalone Sandboxes: the Pod and volume
shape is copied from the namespace's `SandboxTemplate` at creation, so the manifest stays the one
place the runner Pod is defined, and no claim or warm pool sits in between.
"""

from __future__ import annotations

import asyncio
import secrets
import string
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from util.kubernetes import CustomObjectsClient

MANAGED_LABEL = "agentplane.allegedly.works/managed"
PROVIDER_LABEL = "agentplane.allegedly.works/provider"
ARCHIVED_LABEL = "agentplane.allegedly.works/archived"
MODEL_ANNOTATION = "agentplane.allegedly.works/model"

_TEMPLATE_API = ("extensions.agents.x-k8s.io", "v1beta1")
_TEMPLATES_PLURAL = "sandboxtemplates"
_SANDBOX_API = ("agents.x-k8s.io", "v1beta1")
_SANDBOXES_PLURAL = "sandboxes"
_MERGE_PATCH = "application/merge-patch+json"

# Five lowercase alphanumerics, like `generateName`; the slug bound keeps the name a DNS label.
_SUFFIX_LENGTH = 5
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits
_SLUG_MAX_LENGTH = 63 - 1 - _SUFFIX_LENGTH

Slug = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", min_length=1, max_length=_SLUG_MAX_LENGTH)
]


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class OperatingMode(StrEnum):
    RUNNING = "Running"
    SUSPENDED = "Suspended"


class ProvisioningState(StrEnum):
    WAITING_FOR_POD = "waiting_for_pod"
    WAITING_FOR_POD_READY = "waiting_for_pod_ready"
    RUNNING = "running"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class InventoryError(Exception):
    """Base of the errors the API maps to status codes."""


class SandboxNotFoundError(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no Agentplane sandbox {name=}")
        self.name = name


class NewSandbox(BaseModel):
    """What a caller decides about a sandbox; everything else is the namespace's template."""

    model_config = ConfigDict(extra="forbid")

    slug: Slug = Field(description="Human-chosen name stem; a random suffix makes the Sandbox name unique.")
    provider: Provider
    model: str = Field(
        min_length=1, description="Model name the runner passes to the harness; recorded as an annotation."
    )


class Condition(BaseModel):
    """A Kubernetes status condition, as the Sandbox controller and the kubelet report them."""

    model_config = ConfigDict(extra="ignore")

    type: str
    status: str
    reason: str | None = None
    message: str | None = None


class ContainerStatus(BaseModel):
    """One container of the Pod: which of the kubelet's three states it is in, and why."""

    model_config = ConfigDict(extra="forbid")

    name: str
    state: str = Field(description="waiting, running, or terminated.")
    reason: str | None = None
    message: str | None = None
    ready: bool
    restart_count: int


class PodStatus(BaseModel):
    """What the kubelet says about the Sandbox's Pod; absent while no Pod exists."""

    model_config = ConfigDict(extra="forbid")

    phase: str | None
    ip: str | None
    node_name: str | None
    reason: str | None = None
    message: str | None = None
    conditions: list[Condition]
    containers: list[ContainerStatus]


class SandboxView(BaseModel):
    """One inventory row: the Sandbox's identity plus what it and its Pod say."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The Sandbox name, and its Pod's; the handle for every operation.")
    provider: Provider
    model: str
    archived: bool
    state: ProvisioningState
    created_at: datetime
    operating_mode: OperatingMode
    conditions: list[Condition] = Field(description="The Sandbox's own status conditions.")
    node_name: str | None = Field(default=None, description="Where the Sandbox controller placed the Pod.")
    pod: PodStatus | None = None


# Kubernetes-boundary models: the subset of each CR the inventory reads, parsed once off the wire.


class _ObjectMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    creation_timestamp: datetime = Field(alias="creationTimestamp")


class _SandboxSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # The CRD defaults `operatingMode` to Running, so a stored Sandbox without it is a running one.
    operating_mode: OperatingMode = Field(alias="operatingMode", default=OperatingMode.RUNNING)


class _SandboxStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conditions: list[Condition] = Field(default_factory=list)
    node_name: str | None = Field(alias="nodeName", default=None)


class _Sandbox(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _ObjectMeta
    spec: _SandboxSpec
    status: _SandboxStatus = Field(default_factory=_SandboxStatus)


class _TemplateSpec(BaseModel):
    """The parts of a SandboxTemplate a standalone Sandbox carries verbatim."""

    model_config = ConfigDict(extra="ignore")

    pod_template: dict[str, object] = Field(alias="podTemplate")
    volume_claim_templates: list[dict[str, object]] = Field(alias="volumeClaimTemplates", default_factory=list)


class _Template(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spec: _TemplateSpec


class _ResourceList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[dict[str, object]]


class SandboxInventory:
    def __init__(self, *, namespace: str, template: str, custom_objects: CustomObjectsClient, core_v1: CoreV1Api):
        self._namespace = namespace
        self._template = template
        self._custom_objects = custom_objects
        self._core_v1 = core_v1

    async def list_sandboxes(self, *, include_archived: bool = False) -> list[SandboxView]:
        sandboxes_page, pods = await asyncio.gather(
            self._custom_objects.list_namespaced_custom_object(
                *_SANDBOX_API, self._namespace, _SANDBOXES_PLURAL, label_selector=f"{MANAGED_LABEL}=true"
            ),
            self._core_v1.list_namespaced_pod(self._namespace),
        )
        pods_by_name = {pod.metadata.name: pod for pod in pods.items}
        views = []
        for item in _ResourceList.model_validate(sandboxes_page).items:
            sandbox = _Sandbox.model_validate(item)
            views.append(_view(sandbox, pods_by_name.get(sandbox.metadata.name)))
        if include_archived:
            return views
        return [view for view in views if not view.archived]

    async def get(self, name: str) -> SandboxView:
        sandbox = await self._sandbox(name)
        return _view(sandbox, await self._pod(name))

    async def create(self, spec: NewSandbox) -> SandboxView:
        template = _Template.model_validate(
            await self._custom_objects.get_namespaced_custom_object(
                *_TEMPLATE_API, self._namespace, _TEMPLATES_PLURAL, self._template
            )
        )
        suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))
        body = {
            "apiVersion": f"{_SANDBOX_API[0]}/{_SANDBOX_API[1]}",
            "kind": "Sandbox",
            "metadata": {
                "name": f"{spec.slug}-{suffix}",
                "labels": {MANAGED_LABEL: "true", PROVIDER_LABEL: spec.provider},
                "annotations": {MODEL_ANNOTATION: spec.model},
            },
            # No shutdownTime and Retain: the app owns deletion, nothing expires a sandbox behind it.
            "spec": {
                "podTemplate": template.spec.pod_template,
                "volumeClaimTemplates": template.spec.volume_claim_templates,
                "shutdownPolicy": "Retain",
            },
        }
        created = await self._custom_objects.create_namespaced_custom_object(
            *_SANDBOX_API, self._namespace, _SANDBOXES_PLURAL, body
        )
        return _view(_Sandbox.model_validate(created), None)

    async def suspend(self, name: str) -> None:
        await self._set_operating_mode(name, OperatingMode.SUSPENDED)

    async def resume(self, name: str) -> None:
        await self._set_operating_mode(name, OperatingMode.RUNNING)

    async def archive(self, name: str) -> None:
        # Suspend before labelling: if the label write fails, a suspended sandbox is still a valid
        # state, whereas a labelled-but-running one would hide a live Pod from the active list.
        await self.suspend(name)
        await self._patch(name, {"metadata": {"labels": {ARCHIVED_LABEL: "true"}}})

    async def unarchive(self, name: str) -> None:
        """Return the sandbox to the active list; it stays suspended until resumed explicitly."""
        await self._sandbox(name)
        await self._patch(name, {"metadata": {"labels": {ARCHIVED_LABEL: None}}})

    async def delete(self, name: str) -> None:
        """Delete the Sandbox; the controller removes its Pod and PVC, and with them the history."""
        await self._sandbox(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *_SANDBOX_API, self._namespace, _SANDBOXES_PLURAL, name, body=k8s_client.V1DeleteOptions()
        )

    async def _set_operating_mode(self, name: str, mode: OperatingMode) -> None:
        await self._sandbox(name)
        await self._patch(name, {"spec": {"operatingMode": mode}})

    async def _patch(self, name: str, patch: dict[str, object]) -> None:
        await self._custom_objects.patch_namespaced_custom_object(
            *_SANDBOX_API, self._namespace, _SANDBOXES_PLURAL, name, patch, _content_type=_MERGE_PATCH
        )

    async def _sandbox(self, name: str) -> _Sandbox:
        """The named Sandbox, only if it is Agentplane's: an unmanaged one is not in this inventory."""
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                *_SANDBOX_API, self._namespace, _SANDBOXES_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise SandboxNotFoundError(name) from error
            raise
        sandbox = _Sandbox.model_validate(raw)
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


def _view(sandbox: _Sandbox, pod: k8s_client.V1Pod | None) -> SandboxView:
    labels = sandbox.metadata.labels
    archived = labels.get(ARCHIVED_LABEL) == "true"
    return SandboxView(
        name=sandbox.metadata.name,
        provider=Provider(labels[PROVIDER_LABEL]),
        model=sandbox.metadata.annotations[MODEL_ANNOTATION],
        archived=archived,
        state=_state(sandbox, pod, archived=archived),
        created_at=sandbox.metadata.creation_timestamp,
        operating_mode=sandbox.spec.operating_mode,
        conditions=sandbox.status.conditions,
        node_name=sandbox.status.node_name,
        pod=_pod_status(pod) if pod is not None else None,
    )


def _pod_status(pod: k8s_client.V1Pod) -> PodStatus:
    status = pod.status if pod.status is not None else k8s_client.V1PodStatus()
    return PodStatus(
        phase=status.phase,
        ip=status.pod_ip,
        node_name=pod.spec.node_name if pod.spec is not None else None,
        reason=status.reason,
        message=status.message,
        conditions=[
            Condition(type=condition.type, status=condition.status, reason=condition.reason, message=condition.message)
            for condition in status.conditions or []
        ],
        containers=[_container_status(container) for container in status.container_statuses or []],
    )


def _container_status(container: k8s_client.V1ContainerStatus) -> ContainerStatus:
    # Exactly one of the three is set by the kubelet; a status with none is a container not yet scheduled.
    state = container.state if container.state is not None else k8s_client.V1ContainerState()
    if state.waiting is not None:
        name, reason, message = "waiting", state.waiting.reason, state.waiting.message
    elif state.terminated is not None:
        name, reason, message = "terminated", state.terminated.reason, state.terminated.message
    elif state.running is not None:
        name, reason, message = "running", None, None
    else:
        name, reason, message = "waiting", None, None
    return ContainerStatus(
        name=container.name,
        state=name,
        reason=reason,
        message=message,
        ready=container.ready,
        restart_count=container.restart_count,
    )


def _state(sandbox: _Sandbox, pod: k8s_client.V1Pod | None, *, archived: bool) -> ProvisioningState:
    if archived:
        return ProvisioningState.ARCHIVED
    if sandbox.spec.operating_mode == OperatingMode.SUSPENDED:
        return ProvisioningState.SUSPENDED
    if pod is None:
        return ProvisioningState.WAITING_FOR_POD
    return ProvisioningState.RUNNING if _pod_ready(pod) else ProvisioningState.WAITING_FOR_POD_READY


def _pod_ready(pod: k8s_client.V1Pod) -> bool:
    if pod.status is None or pod.status.conditions is None:
        return False
    return any(condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions)
