"""Sandbox creation: the concrete request the service accepts, over Kubernetes-owned state.

The app may select form presets, but this service owns what a new Sandbox is bound to.
"""

import re
from collections.abc import Set
from dataclasses import dataclass

from agentplane.proto import runner_pb2
from agentplane.proto.sandbox_service_pb2 import CreateSandboxRequest, Sandbox
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.inventory import SandboxInventory
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
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", spec.name) or len(spec.name) > 57 or not spec.template:
            raise ValueError("valid name and template are required")
        if max(len(spec.bootstrap), len(spec.session_defaults.setup_script)) > 65_536:
            raise ValueError("script exceeds 65536 characters")
        if spec.session_defaults.HasField("harness") and spec.session_defaults.harness not in (
            runner_pb2.HARNESS_CLAUDE,
            runner_pb2.HARNESS_CODEX,
        ):
            raise ValueError("unsupported harness")
        existing = await self.inventory.retry(spec, caller=caller)
        if existing is not None:
            return existing
        policy_sets = {*self.default_policy_sets, *spec.policy_sets}
        await self.inventory.require_policy_sets(policy_sets)
        return await self.inventory.create(spec, policy_sets=policy_sets, caller=caller)
