"""Agentplane's sandbox inventory: the labelled SandboxClaims in one namespace and what is under them.

Kubernetes is the inventory in this slice — the app persists nothing of its own — so every fact the
app knows about a sandbox is a label or annotation on its claim, and the provisioning state is
derived from the claim, the adopted Sandbox, and the Pod. A claim references a `SandboxWarmPool`,
never a template directly: that is the only reference the pinned Agent Sandbox (v0.5.5) claim
schema accepts, and the pool's `sandboxTemplateRef` is where the runner template is named.
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

_CLAIM_API = ("extensions.agents.x-k8s.io", "v1beta1")
_CLAIMS_PLURAL = "sandboxclaims"
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
    # The claim exists and the controller has not reported on it yet (no status conditions).
    CLAIM_CREATED = "claim_created"
    WAITING_FOR_SANDBOX = "waiting_for_sandbox"
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


class SandboxNotProvisionedError(InventoryError):
    """The claim has no Sandbox yet, so there is nothing to suspend or resume."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name=} has no Sandbox assigned yet")
        self.name = name


class NewSandbox(BaseModel):
    """What a caller decides about a sandbox; everything else is the namespace's configuration."""

    model_config = ConfigDict(extra="forbid")

    slug: Slug = Field(description="Human-chosen name stem; a random suffix makes the claim name unique.")
    provider: Provider
    model: str = Field(
        min_length=1, description="Model name the runner passes to the harness; recorded as an annotation."
    )


class SandboxView(BaseModel):
    """One inventory row: the claim's identity plus what the CR graph beneath it says."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The SandboxClaim name; the handle for every operation.")
    provider: Provider
    model: str
    archived: bool
    state: ProvisioningState
    created_at: datetime
    sandbox_name: str | None = None
    pod_name: str | None = None
    pod_phase: str | None = None
    pod_ip: str | None = None


# Kubernetes-boundary models: the subset of each CR the inventory reads, parsed once off the wire.


class _ObjectMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    creation_timestamp: datetime = Field(alias="creationTimestamp")


class _Condition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    status: str


class _ClaimSandboxStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class _ClaimStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conditions: list[_Condition] = Field(default_factory=list)
    sandbox: _ClaimSandboxStatus | None = None


class _Claim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _ObjectMeta
    status: _ClaimStatus | None = None

    @property
    def sandbox_name(self) -> str | None:
        if self.status is None or self.status.sandbox is None:
            return None
        return self.status.sandbox.name


class _SandboxSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # The CRD defaults `operatingMode` to Running, so a stored Sandbox without it is a running one.
    operating_mode: OperatingMode = Field(alias="operatingMode", default=OperatingMode.RUNNING)


class _Sandbox(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _ObjectMeta
    spec: _SandboxSpec


class _ResourceList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[dict[str, object]]


class SandboxInventory:
    def __init__(self, *, namespace: str, warm_pool: str, custom_objects: CustomObjectsClient, core_v1: CoreV1Api):
        self._namespace = namespace
        self._warm_pool = warm_pool
        self._custom_objects = custom_objects
        self._core_v1 = core_v1

    async def list_sandboxes(self, *, include_archived: bool = False) -> list[SandboxView]:
        claims_page, sandboxes_page, pods = await asyncio.gather(
            self._custom_objects.list_namespaced_custom_object(
                *_CLAIM_API, self._namespace, _CLAIMS_PLURAL, label_selector=f"{MANAGED_LABEL}=true"
            ),
            self._custom_objects.list_namespaced_custom_object(*_SANDBOX_API, self._namespace, _SANDBOXES_PLURAL),
            self._core_v1.list_namespaced_pod(self._namespace),
        )
        sandboxes = {
            sandbox.metadata.name: sandbox
            for sandbox in (
                _Sandbox.model_validate(item) for item in _ResourceList.model_validate(sandboxes_page).items
            )
        }
        pods_by_name = {pod.metadata.name: pod for pod in pods.items}
        views = []
        for item in _ResourceList.model_validate(claims_page).items:
            claim = _Claim.model_validate(item)
            sandbox = sandboxes.get(claim.sandbox_name) if claim.sandbox_name is not None else None
            pod = pods_by_name.get(sandbox.metadata.name) if sandbox is not None else None
            views.append(_view(claim, sandbox, pod))
        if include_archived:
            return views
        return [view for view in views if not view.archived]

    async def get(self, name: str) -> SandboxView:
        claim = await self._claim(name)
        sandbox = await self._sandbox(claim.sandbox_name) if claim.sandbox_name is not None else None
        pod = await self._pod(sandbox.metadata.name) if sandbox is not None else None
        return _view(claim, sandbox, pod)

    async def create(self, spec: NewSandbox) -> SandboxView:
        suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))
        body = {
            "apiVersion": f"{_CLAIM_API[0]}/{_CLAIM_API[1]}",
            "kind": "SandboxClaim",
            "metadata": {
                "name": f"{spec.slug}-{suffix}",
                "labels": {MANAGED_LABEL: "true", PROVIDER_LABEL: spec.provider},
                "annotations": {MODEL_ANNOTATION: spec.model},
            },
            # No shutdownTime and Retain: the app owns deletion, nothing expires a sandbox behind it.
            "spec": {"warmPoolRef": {"name": self._warm_pool}, "lifecycle": {"shutdownPolicy": "Retain"}},
        }
        created = await self._custom_objects.create_namespaced_custom_object(
            *_CLAIM_API, self._namespace, _CLAIMS_PLURAL, body
        )
        return _view(_Claim.model_validate(created), None, None)

    async def suspend(self, name: str) -> None:
        await self._set_operating_mode(name, OperatingMode.SUSPENDED)

    async def resume(self, name: str) -> None:
        await self._set_operating_mode(name, OperatingMode.RUNNING)

    async def archive(self, name: str) -> None:
        # Suspend before labelling: if the label write fails, a suspended sandbox is still a valid
        # state, whereas a labelled-but-running one would hide a live Pod from the active list.
        await self.suspend(name)
        await self._patch_claim(name, {"metadata": {"labels": {ARCHIVED_LABEL: "true"}}})

    async def unarchive(self, name: str) -> None:
        """Return the sandbox to the active list; it stays suspended until resumed explicitly."""
        await self._claim(name)
        await self._patch_claim(name, {"metadata": {"labels": {ARCHIVED_LABEL: None}}})

    async def delete(self, name: str) -> None:
        """Delete the claim; the controller removes the Sandbox and its PVC, and with them the history."""
        await self._claim(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *_CLAIM_API, self._namespace, _CLAIMS_PLURAL, name, body=k8s_client.V1DeleteOptions()
        )

    async def _set_operating_mode(self, name: str, mode: OperatingMode) -> None:
        claim = await self._claim(name)
        if claim.sandbox_name is None:
            raise SandboxNotProvisionedError(name)
        await self._custom_objects.patch_namespaced_custom_object(
            *_SANDBOX_API,
            self._namespace,
            _SANDBOXES_PLURAL,
            claim.sandbox_name,
            {"spec": {"operatingMode": mode}},
            _content_type=_MERGE_PATCH,
        )

    async def _patch_claim(self, name: str, patch: dict[str, object]) -> None:
        await self._custom_objects.patch_namespaced_custom_object(
            *_CLAIM_API, self._namespace, _CLAIMS_PLURAL, name, patch, _content_type=_MERGE_PATCH
        )

    async def _claim(self, name: str) -> _Claim:
        """The named claim, only if it is Agentplane's: an unmanaged claim is not in this inventory."""
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                *_CLAIM_API, self._namespace, _CLAIMS_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise SandboxNotFoundError(name) from error
            raise
        claim = _Claim.model_validate(raw)
        if claim.metadata.labels.get(MANAGED_LABEL) != "true":
            raise SandboxNotFoundError(name)
        return claim

    async def _sandbox(self, name: str) -> _Sandbox | None:
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                *_SANDBOX_API, self._namespace, _SANDBOXES_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                return None
            raise
        return _Sandbox.model_validate(raw)

    async def _pod(self, name: str) -> k8s_client.V1Pod | None:
        try:
            return await self._core_v1.read_namespaced_pod(name, self._namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                return None
            raise


def _view(claim: _Claim, sandbox: _Sandbox | None, pod: k8s_client.V1Pod | None) -> SandboxView:
    labels = claim.metadata.labels
    archived = labels.get(ARCHIVED_LABEL) == "true"
    return SandboxView(
        name=claim.metadata.name,
        provider=Provider(labels[PROVIDER_LABEL]),
        model=claim.metadata.annotations[MODEL_ANNOTATION],
        archived=archived,
        state=_state(claim, sandbox, pod, archived=archived),
        created_at=claim.metadata.creation_timestamp,
        sandbox_name=claim.sandbox_name,
        pod_name=pod.metadata.name if pod is not None else None,
        pod_phase=pod.status.phase if pod is not None and pod.status is not None else None,
        pod_ip=pod.status.pod_ip if pod is not None and pod.status is not None else None,
    )


def _state(
    claim: _Claim, sandbox: _Sandbox | None, pod: k8s_client.V1Pod | None, *, archived: bool
) -> ProvisioningState:
    if archived:
        return ProvisioningState.ARCHIVED
    if claim.status is None or not claim.status.conditions:
        return ProvisioningState.CLAIM_CREATED
    if sandbox is None:
        return ProvisioningState.WAITING_FOR_SANDBOX
    if sandbox.spec.operating_mode == OperatingMode.SUSPENDED:
        return ProvisioningState.SUSPENDED
    if pod is None:
        return ProvisioningState.WAITING_FOR_POD
    return ProvisioningState.RUNNING if _pod_ready(pod) else ProvisioningState.WAITING_FOR_POD_READY


def _pod_ready(pod: k8s_client.V1Pod) -> bool:
    if pod.status is None or pod.status.conditions is None:
        return False
    return any(condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions)
