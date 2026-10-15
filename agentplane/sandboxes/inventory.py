"""Agentplane's sandbox inventory: the Sandboxes in one namespace and the Pod under each.

Kubernetes is the inventory in this slice — this component persists no private database — so every
fact it knows about a sandbox is on its Sandbox. It projects the Sandbox status and its same-name
Pod status separately, without deriving a combined provisioning state. A Sandbox names its
`SandboxTemplate` and policy sets; the Sandbox Controller does the rest.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Set

from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client
from pydantic import BaseModel, ConfigDict
from tenacity import retry, retry_if_exception, stop_after_attempt

from agentplane import sandbox_resource
from agentplane.crd_group import GROUP, VERSION
from agentplane.policy_sets import POLICY_SETS_PLURAL
from agentplane.proto.sandboxes_pb2 import CreateSandboxRequest, Sandbox
from agentplane.sandbox_resource import OperatingMode
from agentplane.sandboxes.console import stream_console
from agentplane.sandboxes.errors import (
    InvalidSandboxError,
    SandboxConflictError,
    SandboxNotFoundError,
    SandboxQuiescingError,
    SandboxRunningError,
    SandboxTearingDownError,
    UnknownPolicySetError,
)
from agentplane.sandboxes.kubernetes_views import CREATE_INTENT, sandbox_views
from agentplane.sandboxes.reader import SandboxReader
from agentplane.subjects import ServiceAccountRef

_MERGE_PATCH = "application/merge-patch+json"


def _is_conflict(error: BaseException) -> bool:
    return isinstance(error, k8s_client.ApiException) and error.status == 409


def _retry_on_conflict[F: Callable[..., Awaitable[object]]](fn: F) -> F:
    """A resourceVersion guard failed: another writer changed the Sandbox between our read and write.
    Read it again and decide again; the decision must see what the other writer committed."""
    # Annotated: tenacity's keyword-configured decorator does not carry the wrapped signature.
    retried: F = retry(retry=retry_if_exception(_is_conflict), stop=stop_after_attempt(5), reraise=True)(fn)
    return retried


class _NamedMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class _NamedResource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _NamedMetadata


class _ResourceList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[dict[str, object]]


class SandboxInventory(SandboxReader):
    async def _names(self, plural: str) -> set[str]:
        listed = await self._custom_objects.list_namespaced_custom_object(GROUP, VERSION, self._namespace, plural)
        return {
            _NamedResource.model_validate(item).metadata.name for item in _ResourceList.model_validate(listed).items
        }

    async def list_templates(self) -> list[str]:
        """The concrete templates an operator may choose for one Sandbox."""
        return sorted(await self._names(sandbox_resource.TEMPLATES_PLURAL))

    async def list_sandboxes(self) -> list[Sandbox]:
        sandboxes_page, pods = await asyncio.gather(
            self._custom_objects.list_namespaced_custom_object(
                GROUP, VERSION, self._namespace, sandbox_resource.SANDBOXES_PLURAL
            ),
            self._core_v1.list_namespaced_pod(self._namespace),
        )
        # kubernetes_asyncio exposes this runtime attribute but omits it from generated SDK stubs.
        api_client = self._core_v1.api_client  # type: ignore[attr-defined]
        return sandbox_views(_ResourceList.model_validate(sandboxes_page).items, pods.items, api_client=api_client)

    def console(self, view: Sandbox) -> AsyncIterator[bytes]:
        """The serial console of `view`'s VM, which is named for the Sandbox."""
        # kubernetes_asyncio exposes this runtime attribute but omits it from generated SDK stubs.
        configuration = self._core_v1.api_client.configuration  # type: ignore[attr-defined]
        return stream_console(configuration, namespace=self._namespace, vmi=view.name)

    async def retry(self, spec: CreateSandboxRequest, *, caller: ServiceAccountRef) -> Sandbox | None:
        """Find the recorded Create before resolving mutable template/policy catalogues. This
        immutable receipt distinguishes a retry from another caller's same-name Create."""
        existing = await sandbox_resource.read_sandbox(self._custom_objects, self._namespace, spec.name)
        if existing is None:
            return None
        if existing.metadata.deletion_timestamp or existing.metadata.annotations.get(CREATE_INTENT) != self._intent(
            spec, caller
        ):
            raise SandboxConflictError(spec.name)
        return await self.get(spec.name)

    async def require_policy_sets(self, names: Set[str]) -> None:
        if missing := sorted(names - await self._names(POLICY_SETS_PLURAL)):
            raise UnknownPolicySetError(missing, kind="PolicySet")

    async def create_validated(
        self, spec: CreateSandboxRequest, *, default_policy_sets: Set[str], caller: ServiceAccountRef
    ) -> Sandbox:
        """Create what `spec` asks for, or return the Sandbox an earlier identical Create made."""
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", spec.name) or len(spec.name) > 57 or not spec.template:
            raise InvalidSandboxError("valid name and template are required")
        if spec.HasField("preset") and not re.fullmatch(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?", spec.preset):
            raise InvalidSandboxError("preset must be a valid label value")
        if len(spec.bootstrap) > 65_536:
            raise InvalidSandboxError("script exceeds 65536 characters")
        existing = await self.retry(spec, caller=caller)
        if existing is not None:
            return existing
        policy_sets = {*default_policy_sets, *spec.policy_sets}
        await self.require_policy_sets(policy_sets)
        return await self.create(spec, policy_sets=policy_sets, caller=caller)

    async def create(self, spec: CreateSandboxRequest, *, policy_sets: Set[str], caller: ServiceAccountRef) -> Sandbox:
        """A Sandbox running from the start: the Sandbox Controller makes its ServiceAccount and
        `PolicyBinding` beside its Pod, so nothing here waits for grants. `retry` has already found no
        Sandbox of the name."""
        body = {
            "apiVersion": sandbox_resource.SANDBOX_API_VERSION,
            "kind": sandbox_resource.SANDBOX_KIND,
            "metadata": {
                "name": spec.name,
                "annotations": {CREATE_INTENT: self._intent(spec, caller)},
                **({"labels": {sandbox_resource.PRESET_LABEL: spec.preset}} if spec.HasField("preset") else {}),
            },
            "spec": {
                "template": spec.template,
                "running": True,
                "policySets": sorted(policy_sets),
                **({"bootstrap": spec.bootstrap} if spec.bootstrap else {}),
            },
        }
        try:
            await self._custom_objects.create_namespaced_custom_object(
                GROUP, VERSION, self._namespace, sandbox_resource.SANDBOXES_PLURAL, body
            )
        except Exception:
            # Even a transport error may mean the create committed. Never clean up a same-name
            # object here; the persisted receipt decides.
            if await self.retry(spec, caller=caller) is None:
                raise
        return await self.get(spec.name)

    @staticmethod
    def _intent(spec: CreateSandboxRequest, caller: ServiceAccountRef) -> str:
        return json.dumps(
            {"caller": caller.model_dump(), "request": MessageToDict(spec, preserving_proto_field_name=True)},
            sort_keys=True,
            separators=(",", ":"),
        )

    async def suspend(self, name: str, *, uid: str) -> None:
        await self._set_running(name, False, uid=uid)

    async def resume(self, name: str, *, uid: str) -> None:
        await self._set_running(name, True, uid=uid)

    @_retry_on_conflict
    async def wake_for_send(
        self, name: str, *, uid: str, before_resume: Callable[[int], Awaitable[None]] | None = None
    ) -> bool:
        """Request the shared guarded resume only when this Sandbox explicitly allows send wakes.

        `False` means the durable command should stay queued. A true result can still be waiting for
        the controller to create a Ready incarnation; callers must continue watching the Sandbox.
        """
        sandbox = await self._incarnation(name, uid=uid)
        if sandbox.metadata.deletion_timestamp is not None:
            raise SandboxTearingDownError(name)
        if not sandbox.spec.wake_on_send:
            return False
        if sandbox.running_reason() == "Quiescing":
            raise SandboxQuiescingError(name)
        if sandbox.operating_mode is OperatingMode.SUSPENDED:
            await self._request_resume(sandbox, name=name, uid=uid, before_resume=before_resume)
        return True

    @_retry_on_conflict
    async def begin_teardown(self, name: str, *, uid: str) -> None:
        """Start deleting a suspended Sandbox, which deletes its volume and everything on it. A running
        one is refused, so the irreversible step is a deliberate second one for a browser and for an
        agent calling the API alike. Asking again changes nothing. The Sandbox Controller runs it
        once more, so that its runner can seal every Session, before it deletes anything."""
        sandbox = await self._incarnation(name, uid=uid)
        if sandbox.metadata.deletion_timestamp is not None:
            return
        if sandbox.operating_mode is OperatingMode.RUNNING:
            raise SandboxRunningError(name)
        await self._custom_objects.delete_namespaced_custom_object(
            GROUP,
            VERSION,
            self._namespace,
            sandbox_resource.SANDBOXES_PLURAL,
            name,
            body=k8s_client.V1DeleteOptions(
                preconditions=k8s_client.V1Preconditions(uid=uid, resource_version=sandbox.metadata.resource_version)
            ),
        )

    async def _incarnation(self, name: str, *, uid: str) -> sandbox_resource.Sandbox:
        sandbox = await sandbox_resource.read_sandbox(self._custom_objects, self._namespace, name)
        if sandbox is None or str(sandbox.metadata.uid) != uid:
            raise SandboxNotFoundError(name)
        return sandbox

    @_retry_on_conflict
    async def _set_running(self, name: str, running: bool, *, uid: str) -> None:
        sandbox = await self._incarnation(name, uid=uid)
        if sandbox.metadata.deletion_timestamp is not None:
            raise SandboxTearingDownError(name)
        if running and sandbox.running_reason() == "Quiescing":
            raise SandboxQuiescingError(name)
        if running:
            if sandbox.operating_mode is OperatingMode.SUSPENDED:
                await self._request_resume(sandbox, name=name, uid=uid)
                return
            if sandbox.spec.running:
                return
            # The status can lag the desired stop, but a resume still has to change desired state.
            spec: dict[str, object] = {"running": True}
        else:
            spec = {"running": False}
        # Guarded, so that a concurrent delete is never answered by a stale read's mode.
        await self._custom_objects.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self._namespace,
            sandbox_resource.SANDBOXES_PLURAL,
            name,
            {"metadata": {"uid": uid, "resourceVersion": sandbox.metadata.resource_version}, "spec": spec},
            _content_type=_MERGE_PATCH,
        )

    async def _request_resume(
        self,
        sandbox: sandbox_resource.Sandbox,
        *,
        name: str,
        uid: str,
        before_resume: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        """Apply the wake verb to an already-read suspended Sandbox; caller owns conflict retry."""
        if sandbox.running_reason() == "Quiescing":
            raise SandboxQuiescingError(name)
        idle_still_suspended = (
            sandbox.status.suspended_by == "Idle"
            and sandbox.status.suspended_at_wake_generation == sandbox.spec.wake_generation
        )
        if sandbox.spec.running and not idle_still_suspended:
            return
        wake_generation = sandbox.spec.wake_generation + 1
        if before_resume is not None:
            await before_resume(wake_generation)
        await self._custom_objects.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self._namespace,
            sandbox_resource.SANDBOXES_PLURAL,
            name,
            {
                "metadata": {"uid": uid, "resourceVersion": sandbox.metadata.resource_version},
                "spec": {"running": True, "wakeGeneration": wake_generation},
            },
            _content_type=_MERGE_PATCH,
        )


# gazelle:include_dep @pypi//protobuf
