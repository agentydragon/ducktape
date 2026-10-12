"""Sandbox provisioning orchestration over existing Kubernetes-owned state.

The app may select form presets, but this service owns concrete grants and launch bindings.
Existing labels/finalizers and deletion semantics are retained for an in-place ownership handoff.
"""

import json
import logging
import re
from collections.abc import Set
from dataclasses import dataclass

from google.protobuf.json_format import MessageToDict
from more_itertools import unique_everseen
from pydantic import BaseModel, ConfigDict, Field

from agentplane.proto import runner_pb2
from agentplane.proto.sandbox_service_pb2 import CreateSandboxRequest, Sandbox, SandboxBinding
from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.binding_storage import write_binding
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KUBERNETES_BINDINGS_FINALIZER, KubernetesBindings
from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant, resolve_grants
from agentplane.sandbox_service.kubernetes_views import (
    KUBERNETES_GRANTS_ANNOTATION,
    PROVISIONING_ANNOTATION,
    SANDBOX_BINDING_ANNOTATION,
)
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoints
from agentplane.sandbox_service.session_config import LaunchGrants
from agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)


class OwnSandboxes(BaseModel):
    """CreateSandbox creates Agentplane's own Sandbox kind, which takes policy sets alone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_policy_sets: Set[str] = Field(description="PolicySets every new Sandbox is bound to.")


@dataclass(frozen=True)
class Provisioning:
    inventory: SandboxInventory
    egress: EgressInventory
    action_policy: ActionPolicyBindings
    grants: dict[str, KubernetesGrant]
    bindings: KubernetesBindings
    endpoints: RunnerEndpoints
    # Absent, CreateSandbox creates agent-sandbox's kind.
    own_sandboxes: OwnSandboxes | None

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
        if self.own_sandboxes is not None:
            return await self._create_own(spec, self.own_sandboxes, caller=caller)
        if spec.policy_sets:
            raise ValueError("policy sets are for Agentplane's own Sandbox kind")
        self.endpoints.require_unreserved(spec.egress_policies)
        existing = await self.inventory.retry(spec, caller=caller)
        if existing is not None:
            return existing
        grants = resolve_grants(list(spec.kubernetes_grants), self.grants)
        policies = list(
            unique_everseen(
                [*self.endpoints.egress_policies(), *self.egress.launch_policies(list(spec.egress_policies))]
            )
        )
        await self.egress.require_policies(policies)
        await self.action_policy.require_policy_sets(list(spec.action_policy_sets))
        binding = (
            SandboxBinding(
                session_defaults=spec.session_defaults if spec.HasField("session_defaults") else None,
                bootstrap=spec.bootstrap,
            )
            if spec.HasField("session_defaults") or spec.bootstrap
            else None
        )
        annotations = {
            PROVISIONING_ANNOTATION: LaunchGrants(
                egress_policies=policies, action_policy_sets=list(spec.action_policy_sets)
            ).model_dump_json()
        }
        if binding is not None:
            annotations[SANDBOX_BINDING_ANNOTATION] = write_binding(binding)
        if grants:
            annotations[KUBERNETES_GRANTS_ANNOTATION] = json.dumps(
                [MessageToDict(grant, preserving_proto_field_name=True) for grant in grants]
            )
        return await self.inventory.create(
            spec,
            caller=caller,
            endpoints=self.endpoints,
            annotations=annotations or None,
            finalizers=[KUBERNETES_BINDINGS_FINALIZER]
            if any(
                grant.grant.kind == "ClusterRoleBinding" or grant.grant.namespace != self.inventory.namespace
                for grant in grants
            )
            else None,
        )

    async def _create_own(self, spec: CreateSandboxRequest, own: OwnSandboxes, *, caller: ServiceAccountRef) -> Sandbox:
        # Its runner reaches Agentplane's services over the egress sidecar's platform routes, so no
        # endpoint policy is granted, and a PolicySet holds any rule type.
        if spec.egress_policies or spec.action_policy_sets or spec.kubernetes_grants:
            raise ValueError("Agentplane's own Sandbox takes policy sets, not egress, Action or Kubernetes grants")
        existing = await self.inventory.retry(spec, caller=caller)
        if existing is not None:
            return existing
        policy_sets = {*own.default_policy_sets, *spec.policy_sets}
        await self.inventory.require_policy_sets(policy_sets)
        return await self.inventory.create_own(spec, policy_sets=policy_sets, caller=caller)

    async def ensure(self, sandbox: Sandbox) -> None:
        if sandbox.deleting:
            return
        initializing = await self.inventory.initialization_pending(sandbox.name)
        if initializing:
            await self.inventory.ensure_service_account(sandbox)
        intent = await self.inventory.pending_grants(sandbox.name)
        if intent is None:
            if initializing:
                await self.inventory.complete_initialization(sandbox)
            return  # Existing staging resources have no new intent to reinterpret or replace.
        if intent.egress_policies:
            await self.egress.grant(sandbox, intent.egress_policies, initial=True)
        if intent.action_policy_sets:
            await self.action_policy.bind(sandbox, intent.action_policy_sets, initial=True)
        if sandbox.kubernetes_grants:
            await self.bindings.ensure(sandbox)
            if not (await self.inventory.get(sandbox.name)).kubernetes_grants_ready:
                return
        await self.inventory.finish_provisioning(sandbox)
        if initializing:
            await self.inventory.complete_initialization(sandbox)

    async def reconcile_once(self) -> None:
        await self.bindings.reconcile_once()
        for sandbox in await self.inventory.list_agent_sandboxes():
            try:
                await self.ensure(sandbox)
            except Exception:
                logger.exception("provisioning incomplete for %s", sandbox.name)
