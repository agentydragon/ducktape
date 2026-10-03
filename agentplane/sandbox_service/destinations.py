"""Resolve an explicit, authorized Sandbox incarnation to its current controller-owned runner Pod."""

from dataclasses import dataclass
from ipaddress import ip_address

from kubernetes_asyncio import client as k8s_client

from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.models import OperatingMode, SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import SandboxBinding, SandboxDestination
from util.agent_sandbox import SANDBOX_API


class DestinationUnavailableError(Exception):
    """The destination has no verified, currently reachable runner Pod. Not permanent removal."""


@dataclass(frozen=True)
class RunnerEndpoint:
    target: str
    binding: SandboxBinding | None


@dataclass(frozen=True)
class DestinationResolver:
    inventory: SandboxInventory
    core: k8s_client.CoreV1Api
    runner_port: int

    async def resolve(self, destination: SandboxDestination) -> RunnerEndpoint:
        if not all((destination.sandbox, destination.sandbox_uid, destination.owner.namespace, destination.owner.name)):
            raise ValueError("Sandbox name, UID, and owner are required")
        view = await self.inventory.get(destination.sandbox)
        if view.uid != destination.sandbox_uid or view.service_account != destination.owner:
            raise SandboxNotFoundError(destination.sandbox)
        if (
            view.deleting
            or view.operating_mode != OperatingMode.RUNNING
            or view.launch_grants_pending
            or not view.kubernetes_grants_ready
            or view.HasField("kubernetes_grant_error")
        ):
            raise DestinationUnavailableError
        try:
            pod = await self.core.read_namespaced_pod(destination.sandbox, self.inventory.namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise DestinationUnavailableError from error
            raise
        metadata, spec, status = pod.metadata, pod.spec, pod.status
        if (
            metadata is None
            or metadata.name != destination.sandbox
            or metadata.namespace != self.inventory.namespace
            or not metadata.uid
            or metadata.deletion_timestamp is not None
            or spec is None
            or (spec.service_account_name or "default") != destination.owner.name
            or status is None
            or status.phase != "Running"
            or not status.pod_ip
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
            or owner.name != destination.sandbox
            or owner.uid != str(destination.sandbox_uid)
        ):
            raise DestinationUnavailableError
        try:
            address = ip_address(status.pod_ip)
        except ValueError as error:
            raise DestinationUnavailableError from error
        host = f"[{address}]" if address.version == 6 else str(address)
        return RunnerEndpoint(
            target=f"{host}:{self.runner_port}", binding=view.binding if view.HasField("binding") else None
        )


# gazelle:include_dep @pypi//protobuf
