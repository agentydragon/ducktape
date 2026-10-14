"""Sandbox creation: the concrete request the service accepts, over Kubernetes-owned state.

The app may select form presets, but this service owns what a new Sandbox is bound to.
"""

from collections.abc import Set
from dataclasses import dataclass

from agentplane.proto.sandbox_service_pb2 import CreateSandboxRequest, Sandbox
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//types_protobuf


@dataclass(frozen=True)
class Provisioning:
    inventory: SandboxInventory
    egress: EgressInventory
    # PolicySets every new Sandbox is bound to, beside the request's.
    default_policy_sets: Set[str]

    async def create(self, spec: CreateSandboxRequest, *, caller: ServiceAccountRef) -> Sandbox:
        return await self.inventory.create_validated(spec, default_policy_sets=self.default_policy_sets, caller=caller)
