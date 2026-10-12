"""Check an explicit, authorized Sandbox incarnation and its current controller-owned runner Pod."""

from dataclasses import dataclass

from kubernetes_asyncio import client as k8s_client

from agentplane.proto.sandbox_service_pb2 import Sandbox, SandboxBinding, SandboxDestination, TeardownPhase
from agentplane.sandbox_service.errors import SandboxTearingDownError
from agentplane.sandbox_service.reader import SandboxReader
from util.agent_sandbox import SANDBOX_API, OperatingMode


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
        if writes and view.teardown != TeardownPhase.TEARDOWN_PHASE_UNSPECIFIED:
            raise SandboxTearingDownError(view.name)
        if (
            view.deleting
            or view.operating_mode != OperatingMode.RUNNING
            or view.launch_grants_pending
            or not view.kubernetes_grants_ready
            or view.HasField("kubernetes_grant_error")
        ):
            raise DestinationUnavailableError
        try:
            pod = await self.core.read_namespaced_pod(view.name, self.sandboxes.namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise DestinationUnavailableError from error
            raise
        metadata, spec, status = pod.metadata, pod.spec, pod.status
        if (
            metadata is None
            or metadata.name != view.name
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
        if (
            owner.api_version != SANDBOX_API.api_version
            or owner.kind != "Sandbox"
            or owner.name != view.name
            or owner.uid != str(view.uid)
        ):
            raise DestinationUnavailableError
        return view.binding if view.HasField("binding") else None


# gazelle:include_dep @pypi//protobuf
