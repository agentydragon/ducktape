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
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api
from pydantic import BaseModel, ConfigDict, Field
from tenacity import retry, retry_if_exception, stop_after_attempt

from agentplane.action_service.policies.resources import CALLER_LABEL
from agentplane.sandbox_service.binding_storage import read_binding
from agentplane.sandbox_service.kubernetes_views import (
    CREATE_INTENT,
    INITIALIZING,
    KUBERNETES_GRANTS_ERROR_ANNOTATION,
    KUBERNETES_GRANTS_READY_ANNOTATION,
    MANAGED_LABEL,
    PROVISIONING_ANNOTATION,
    RETENTION_HOLDS_ANNOTATION,
    SANDBOX_BINDING_ANNOTATION,
    SandboxResource,
    sandbox_view,
    sandbox_views,
)
from agentplane.sandbox_service.models import (
    HoldNotFoundError,
    RetentionHeldError,
    SandboxConflictError,
    SandboxNotFoundError,
    SandboxRunningError,
)
from agentplane.sandbox_service.protocol_pb2 import CreateSandboxRequest, Hold, Sandbox, SandboxBinding
from agentplane.sandbox_service.retention_holds import RetentionHolds, read_holds
from agentplane.sandbox_service.session_config import LaunchGrants
from agentplane.subjects import ServiceAccountRef
from util.agent_sandbox import EXTENSIONS_API, SANDBOX_API, SANDBOXES_PLURAL, TEMPLATES_PLURAL, OperatingMode
from util.kubernetes import CustomObjectsClient

_MERGE_PATCH = "application/merge-patch+json"


def _is_conflict(error: BaseException) -> bool:
    return isinstance(error, k8s_client.ApiException) and error.status == 409


def _retry_on_conflict[F: Callable[..., Awaitable[object]]](fn: F) -> F:
    """A resourceVersion guard failed: another writer changed the Sandbox between our read and write.
    Read it again and decide again; the decision must see what the other writer committed."""
    # Annotated: tenacity's keyword-configured decorator does not carry the wrapped signature.
    retried: F = retry(retry=retry_if_exception(_is_conflict), stop=stop_after_attempt(5), reraise=True)(fn)
    return retried


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

    @property
    def sandbox_list(self) -> Callable[..., Awaitable[dict[str, Any]]]:
        """Use the same Kubernetes list API for a resourceVersion-based watch."""
        return self._custom_objects.list_namespaced_custom_object

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
        caller: ServiceAccountRef,
    ) -> Sandbox:
        # This immutable receipt distinguishes a retry from another caller's same-name Create.
        # Resolve an existing CR before looking up the template: templates may change or disappear.
        intent = self._intent(spec, caller)
        name = spec.name
        try:
            existing = await self._sandbox(name)
        except SandboxNotFoundError:
            existing = None
        if existing is not None:
            self._check_intent(existing, intent)
            return await self.get(name)
        template = _Template.model_validate(
            await self._custom_objects.get_namespaced_custom_object(
                *EXTENSIONS_API, self._namespace, TEMPLATES_PLURAL, spec.template
            )
        )
        body = {
            "apiVersion": SANDBOX_API.api_version,
            "kind": "Sandbox",
            "metadata": {
                "name": name,
                "labels": {MANAGED_LABEL: "true"},
                "annotations": {**(annotations or {}), CREATE_INTENT: intent, INITIALIZING: "true"},
                **({"finalizers": finalizers} if finalizers else {}),
            },
            "spec": {
                "podTemplate": _running_as(template.spec.pod_template, name),
                "volumeClaimTemplates": template.spec.volume_claim_templates,
                "shutdownPolicy": "Retain",
                "operatingMode": "Suspended",
            },
        }
        try:
            created = await self._custom_objects.create_namespaced_custom_object(
                *SANDBOX_API, self._namespace, SANDBOXES_PLURAL, body
            )
        except Exception as error:
            # Even a transport error may mean the create committed. Never clean up a
            # same-name object here; the retry/reconciler checks the persisted receipt.
            try:
                existing = await self._sandbox(name)
            except SandboxNotFoundError:
                raise error
            self._check_intent(existing, intent)
            return await self.get(name)
        sandbox = SandboxResource.model_validate(created)
        # The Pod cannot start until provisioning removes INITIALIZING and resumes it.
        api_client = self._core_v1.api_client  # type: ignore[attr-defined]
        return sandbox_view(sandbox, None, api_client=api_client)

    async def retry(self, spec: CreateSandboxRequest, *, caller: ServiceAccountRef) -> Sandbox | None:
        """Find the recorded Create before resolving mutable template/policy catalogues."""
        try:
            existing = await self._sandbox(spec.name)
        except SandboxNotFoundError:
            return None
        self._check_intent(existing, self._intent(spec, caller))
        return await self.get(spec.name)

    @staticmethod
    def _intent(spec: CreateSandboxRequest, caller: ServiceAccountRef) -> str:
        return json.dumps(
            {"caller": caller.model_dump(), "request": MessageToDict(spec, preserving_proto_field_name=True)},
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _check_intent(sandbox: SandboxResource, intent: str) -> None:
        if sandbox.metadata.deletion_timestamp or sandbox.metadata.annotations.get(CREATE_INTENT) != intent:
            raise SandboxConflictError(sandbox.metadata.name)

    async def initialization_pending(self, name: str) -> bool:
        return INITIALIZING in (await self._sandbox(name)).metadata.annotations

    async def ensure_service_account(self, sandbox: Sandbox) -> None:
        """A CR-first creation leaves no unowned account; compare UIDs before accepting one."""
        owner = k8s_client.V1OwnerReference(
            api_version=SANDBOX_API.api_version,
            kind="Sandbox",
            name=sandbox.name,
            uid=sandbox.uid,
            controller=False,
            block_owner_deletion=False,
        )
        try:
            account = await self._core_v1.read_namespaced_service_account(sandbox.name, self._namespace)
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
        else:
            if (
                (account.metadata.owner_references or []) != [owner]
                or any((account.metadata.labels or {}).get(key) != "true" for key in (MANAGED_LABEL, CALLER_LABEL))
                or account.metadata.deletion_timestamp is not None
            ):
                raise SandboxConflictError(sandbox.name)
            return
        try:
            await self._core_v1.create_namespaced_service_account(
                self._namespace,
                k8s_client.V1ServiceAccount(
                    metadata=k8s_client.V1ObjectMeta(
                        name=sandbox.name,
                        labels={MANAGED_LABEL: "true", CALLER_LABEL: "true"},
                        owner_references=[owner],
                    )
                ),
            )
        except Exception as error:
            # An ambiguous write or concurrent retry: re-read and verify; never adopt.
            try:
                account = await self._core_v1.read_namespaced_service_account(sandbox.name, self._namespace)
            except k8s_client.ApiException as read_error:
                if read_error.status == 404:
                    raise error
                raise
            if (
                (account.metadata.owner_references or []) != [owner]
                or any((account.metadata.labels or {}).get(key) != "true" for key in (MANAGED_LABEL, CALLER_LABEL))
                or account.metadata.deletion_timestamp is not None
            ):
                raise SandboxConflictError(sandbox.name) from None

    async def complete_initialization(self, sandbox: Sandbox) -> None:
        current = await self._sandbox(sandbox.name)
        if current.metadata.uid != sandbox.uid or current.metadata.deletion_timestamp:
            raise SandboxConflictError(sandbox.name)
        if INITIALIZING not in current.metadata.annotations:
            return
        if PROVISIONING_ANNOTATION in current.metadata.annotations:
            return
        if current.spec.operating_mode != OperatingMode.SUSPENDED:
            raise SandboxConflictError(sandbox.name)
        # One conditional patch changes both fields: neither a crashed caller nor a
        # competing lifecycle operation can expose a running, unprovisioned Pod.
        await self._patch(
            sandbox.name,
            {
                "metadata": {
                    "uid": sandbox.uid,
                    "resourceVersion": current.metadata.resource_version,
                    "annotations": {INITIALIZING: None},
                },
                "spec": {"operatingMode": "Running"},
            },
        )

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

    @_retry_on_conflict
    async def delete(self, name: str, *, uid: str | None = None) -> None:
        """Delete a suspended Sandbox; the controller removes its Pod and PVC, and with them
        everything on the volume. A running one is refused, so the irreversible step is a
        deliberate second one for a browser and for an agent calling the API alike.

        So is one with an unsatisfied retention hold. The delete is conditional on the
        resourceVersion the holds were read at, so a hold placed concurrently either lands first
        and refuses the deletion, or finds the deletion committed and is refused itself."""
        sandbox = await self._mutable_sandbox(name, uid=uid)
        if sandbox.spec.operating_mode != OperatingMode.SUSPENDED:
            raise SandboxRunningError(name)
        if self._holds(sandbox).blocking():
            raise RetentionHeldError(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *SANDBOX_API,
            self._namespace,
            SANDBOXES_PLURAL,
            name,
            body=k8s_client.V1DeleteOptions(
                preconditions=k8s_client.V1Preconditions(
                    uid=str(sandbox.metadata.uid), resource_version=sandbox.metadata.resource_version
                )
            ),
        )

    @_retry_on_conflict
    async def place_hold(self, name: str, *, uid: str, session_id: str, holder: str) -> Hold:
        sandbox = await self._incarnation(name, uid=uid)
        if sandbox.metadata.deletion_timestamp is not None:
            raise SandboxNotFoundError(name)  # Deletion is committed; nothing can hold it back now.
        held = await self._write_holds(sandbox, self._holds(sandbox).place(session_id, holder))
        return self._held(held, session_id, holder)

    @_retry_on_conflict
    async def confirm_hold(self, name: str, *, uid: str, session_id: str, holder: str, through_cursor: int) -> Hold:
        sandbox = await self._incarnation(name, uid=uid)
        holds = self._holds(sandbox)
        self._held(holds, session_id, holder)
        return self._held(
            await self._write_holds(sandbox, holds.confirm(session_id, holder, through_cursor)), session_id, holder
        )

    @_retry_on_conflict
    async def release_hold(self, name: str, *, uid: str, session_id: str, holder: str) -> None:
        sandbox = await self._incarnation(name, uid=uid)
        await self._write_holds(sandbox, self._holds(sandbox).release(session_id, holder))

    async def list_holds(self, name: str, *, uid: str) -> list[Hold]:
        return self._holds(await self._incarnation(name, uid=uid)).holds()

    @staticmethod
    def _holds(sandbox: SandboxResource) -> RetentionHolds:
        return read_holds(sandbox.metadata.annotations.get(RETENTION_HOLDS_ANNOTATION))

    @staticmethod
    def _held(holds: RetentionHolds, session_id: str, holder: str) -> Hold:
        hold = holds.hold(session_id, holder)
        if hold is None:
            raise HoldNotFoundError(session_id, holder)
        return hold

    async def _write_holds(self, sandbox: SandboxResource, holds: RetentionHolds) -> RetentionHolds:
        """Write only a change, guarded by the resourceVersion the holds were read at."""
        if holds == self._holds(sandbox):
            return holds
        try:
            await self._patch(
                sandbox.metadata.name,
                {
                    "metadata": {
                        "uid": sandbox.metadata.uid,
                        "resourceVersion": sandbox.metadata.resource_version,
                        "annotations": {
                            RETENTION_HOLDS_ANNOTATION: holds.model_dump_json() if holds.sessions else None
                        },
                    }
                },
            )
        except k8s_client.ApiException as error:
            if error.status == 404:  # Deleted since it was read.
                raise SandboxNotFoundError(sandbox.metadata.name) from error
            raise
        return holds

    async def _set_operating_mode(self, name: str, mode: OperatingMode, *, uid: str | None = None) -> None:
        sandbox = await self._mutable_sandbox(name, uid=uid)
        await self._patch(name, {"metadata": {"uid": str(sandbox.metadata.uid)}, "spec": {"operatingMode": mode}})

    async def _mutable_sandbox(self, name: str, *, uid: str | None) -> SandboxResource:
        """Require the named incarnation to have finished initialization before lifecycle changes."""
        sandbox = await self._sandbox(name) if uid is None else await self._incarnation(name, uid=uid)
        if INITIALIZING in sandbox.metadata.annotations:
            raise SandboxConflictError(name)
        return sandbox

    async def _incarnation(self, name: str, *, uid: str) -> SandboxResource:
        sandbox = await self._sandbox(name)
        if sandbox.metadata.uid != uid:
            raise SandboxNotFoundError(name)
        return sandbox

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
