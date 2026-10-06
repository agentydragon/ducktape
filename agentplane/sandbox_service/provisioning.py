"""Sandbox provisioning orchestration over existing Kubernetes-owned state.

The app may select form presets, but this service owns concrete grants and launch bindings.
Existing labels/finalizers and deletion semantics are retained for an in-place ownership handoff.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from google.protobuf.json_format import MessageToDict

from agentplane.runner import protocol_pb2 as runner_pb2
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
from agentplane.sandbox_service.protocol_pb2 import CreateSandboxRequest, Sandbox, SandboxBinding
from agentplane.sandbox_service.session_config import LaunchGrants


@dataclass(frozen=True)
class Provisioning:
    inventory: SandboxInventory
    egress: EgressInventory
    action_policy: ActionPolicyBindings
    grants: dict[str, KubernetesGrant]
    bindings: KubernetesBindings

    async def create(self, spec: CreateSandboxRequest) -> Sandbox:
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", spec.slug) or len(spec.slug) > 57 or not spec.template:
            raise ValueError("valid slug and template are required")
        if max(len(spec.bootstrap), len(spec.session_defaults.setup_script)) > 65_536:
            raise ValueError("script exceeds 65536 characters")
        if spec.session_defaults.HasField("harness") and spec.session_defaults.harness not in (
            runner_pb2.HARNESS_CLAUDE,
            runner_pb2.HARNESS_CODEX,
        ):
            raise ValueError("unsupported harness")
        grants = resolve_grants(list(spec.kubernetes_grants), self.grants)
        policies = self.egress.launch_policies(list(spec.egress_policies))
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
                policies=policies, action_policy_sets=list(spec.action_policy_sets)
            ).model_dump_json()
        }
        if binding is not None:
            annotations[SANDBOX_BINDING_ANNOTATION] = write_binding(binding)
        if grants:
            annotations[KUBERNETES_GRANTS_ANNOTATION] = json.dumps(
                [MessageToDict(grant, preserving_proto_field_name=True) for grant in grants]
            )
        view = await self.inventory.create(
            spec,
            annotations=annotations or None,
            finalizers=[KUBERNETES_BINDINGS_FINALIZER]
            if any(
                grant.grant.kind == "ClusterRoleBinding" or grant.grant.namespace != self.inventory.namespace
                for grant in grants
            )
            else None,
        )
        await self.ensure(view)
        return await self.inventory.get(view.name)

    async def ensure(self, sandbox: Sandbox) -> None:
        if sandbox.deleting:
            return
        intent = await self.inventory.pending_grants(sandbox.name)
        if intent is None:
            return  # Existing staging resources have no new intent to reinterpret or replace.
        if intent.policies:
            await self.egress.grant(sandbox, intent.policies, initial=True)
        if intent.action_policy_sets:
            await self.action_policy.bind(sandbox, intent.action_policy_sets, initial=True)
        if sandbox.kubernetes_grants:
            await self.bindings.ensure(sandbox)
            if not (await self.inventory.get(sandbox.name)).kubernetes_grants_ready:
                return
        await self.inventory.finish_provisioning(sandbox)

    async def reconcile_once(self) -> None:
        await self.bindings.reconcile_once()
        for sandbox in await self.inventory.list_sandboxes():
            try:
                await self.ensure(sandbox)
            except Exception:
                logging.getLogger(__name__).exception("provisioning incomplete for %s", sandbox.name)

    async def run(self, *, interval_seconds: float = 30) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception:
                logging.getLogger(__name__).exception("provisioning reconciliation failed")
            await asyncio.sleep(interval_seconds)
