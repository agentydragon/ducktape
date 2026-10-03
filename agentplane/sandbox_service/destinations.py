"""Resolve an explicit, authorized Sandbox incarnation to its current controller-owned runner Pod."""

from dataclasses import dataclass
from ipaddress import ip_address

from kubernetes_asyncio import client as k8s_client

from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kind import from_wire
from agentplane.sandbox_service.kubevirt import pod_owned_by_vmi
from agentplane.sandbox_service.models import EnvironmentKind, ProvisioningState, SandboxNotFoundError
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
        kind = from_wire(destination.kind)
        view = await self.inventory.get(destination.sandbox, kind=kind)
        if view.uid != destination.sandbox_uid or view.service_account != destination.owner:
            raise SandboxNotFoundError(destination.sandbox)
        if view.deleting or view.state != ProvisioningState.RUNNING:
            raise DestinationUnavailableError
        if kind == EnvironmentKind.KUBEVIRT:
            return await self._resolve_vm(destination, view.binding if view.HasField("binding") else None)
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

    async def _resolve_vm(self, destination: SandboxDestination, binding: SandboxBinding | None) -> RunnerEndpoint:
        vmi = await self.inventory.current_vm_instance(destination.sandbox, destination.sandbox_uid)
        if vmi is None or vmi.status.get("phase") != "Running":
            raise DestinationUnavailableError
        pods = await self.core.list_namespaced_pod(self.inventory.namespace)
        matching = [pod for pod in pods.items if pod_owned_by_vmi(pod, vmi)]
        if len(matching) != 1:
            raise DestinationUnavailableError
        pod = matching[0]
        metadata, spec, status = pod.metadata, pod.spec, pod.status
        if (
            metadata is None
            or not metadata.uid
            or metadata.deletion_timestamp is not None
            or spec is None
            or spec.service_account_name != destination.owner.name
            or spec.automount_service_account_token is not False
            or status is None
            or status.phase != "Running"
            or not status.pod_ip
            or not any(c.type == "Ready" and c.status == "True" for c in status.conditions or [])
        ):
            raise DestinationUnavailableError
        relay = [container for container in spec.containers if container.name == "egress-sidecar"]
        if len(relay) != 1 or not any(
            container.name == "egress-sidecar" and container.ready for container in status.container_statuses or []
        ):
            raise DestinationUnavailableError
        tokens = [volume for volume in spec.volumes or [] if volume.name == "agentplane-egress-token"]
        if (
            len(tokens) != 1
            or tokens[0].projected is None
            or not tokens[0].projected.sources
            or not all(source.service_account_token is not None for source in tokens[0].projected.sources)
            or not any(
                mount.name == "agentplane-egress-token" and mount.read_only for mount in relay[0].volume_mounts or []
            )
            or any(
                mount.name == "agentplane-egress-token"
                for container in spec.containers
                if container.name != "egress-sidecar"
                for mount in container.volume_mounts or []
            )
        ):
            raise DestinationUnavailableError
        try:
            address = ip_address(status.pod_ip)
        except ValueError as error:
            raise DestinationUnavailableError from error
        host = f"[{address}]" if address.version == 6 else str(address)
        return RunnerEndpoint(target=f"{host}:{self.runner_port}", binding=binding)


# gazelle:include_dep @pypi//protobuf
