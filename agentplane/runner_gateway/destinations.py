"""Check an explicit, authorized Sandbox incarnation and its current runner Pod: one the Sandbox
controls, or a VM Sandbox's virt-launcher Pod."""

from dataclasses import dataclass

from kubernetes_asyncio import client as k8s_client

from agentplane.proto.sandbox_service_pb2 import Sandbox, SandboxBinding, SandboxDestination, SandboxPhase
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND, SANDBOX_LABEL, VMI_API_VERSION, VMI_KIND
from agentplane.sandboxes.errors import SandboxTearingDownError
from agentplane.sandboxes.reader import SandboxReader

_TEARING_DOWN = {SandboxPhase.SANDBOX_PHASE_QUIESCING, SandboxPhase.SANDBOX_PHASE_TEARING_DOWN}
# A sealing Sandbox runs its runner once more to seal, so its Sessions stay readable.
_RUNNER_REACHABLE = {SandboxPhase.SANDBOX_PHASE_RUNNING, SandboxPhase.SANDBOX_PHASE_QUIESCING}


class DestinationUnavailableError(Exception):
    """The destination has no verified, currently reachable runner Pod. Not permanent removal."""


@dataclass(frozen=True)
class DestinationResolver:
    sandboxes: SandboxReader
    core: k8s_client.CoreV1Api

    async def resolve(self, destination: SandboxDestination, *, writes: bool) -> SandboxBinding | None:
        """The Sandbox's binding, once its runner Pod is running and ready. `writes`: the caller may
        create, resume or command a Session, which a Sandbox being torn down refuses. Listing its
        Sessions stays possible until it is deleted."""
        return await self._ready(await self.sandboxes.incarnation(destination), writes=writes)

    async def _ready(self, view: Sandbox, *, writes: bool) -> SandboxBinding | None:
        if writes and view.phase in _TEARING_DOWN:
            raise SandboxTearingDownError(view.name)
        if view.phase not in _RUNNER_REACHABLE or not view.HasField("pod"):
            raise DestinationUnavailableError
        try:
            pod = await self.core.read_namespaced_pod(view.pod.name, self.sandboxes.namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise DestinationUnavailableError from error
            raise
        metadata, spec, status = pod.metadata, pod.spec, pod.status
        if (
            metadata is None
            or metadata.uid != view.pod.uid
            or metadata.namespace != self.sandboxes.namespace
            or not metadata.uid
            or metadata.deletion_timestamp is not None
            or spec is None
            or (spec.service_account_name or "default") != view.service_account.name
            or status is None
            or status.phase != "Running"
            or not any(c.type == "Ready" and c.status == "True" for c in status.conditions or [])
        ):
            raise DestinationUnavailableError
        controllers = [ref for ref in metadata.owner_references or [] if ref.controller]
        if len(controllers) != 1:
            raise DestinationUnavailableError
        owner = controllers[0]
        if (owner.api_version, owner.kind) == (VMI_API_VERSION, VMI_KIND):
            # The view shows our Sandbox's Pod only while the Sandbox Controller reports it as the
            # incarnation, which for a VM is its VMI's virt-launcher Pod.
            if (metadata.labels or {}).get(SANDBOX_LABEL) != view.name:
                raise DestinationUnavailableError
        elif (
            owner.api_version != SANDBOX_API_VERSION
            or owner.kind != SANDBOX_KIND
            or owner.name != view.name
            or owner.uid != str(view.uid)
        ):
            raise DestinationUnavailableError
        return view.binding if view.HasField("binding") else None


# gazelle:include_dep @pypi//protobuf
