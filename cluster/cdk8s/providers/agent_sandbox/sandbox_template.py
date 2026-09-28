"""Ergonomic wrapper for agent-sandbox's `SandboxTemplate`, following cdk8s-plus's own
construction pattern: a class named after the kind, with keyword parameters mirroring
`SandboxTemplateSpec`'s own fields.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
    SandboxTemplate as _SandboxTemplate,
    SandboxTemplateSpec,
    SandboxTemplateSpecEnvVarsInjectionPolicy,
    SandboxTemplateSpecNetworkPolicy,
    SandboxTemplateSpecNetworkPolicyManagement,
    SandboxTemplateSpecPodTemplate,
    SandboxTemplateSpecVolumeClaimTemplates,
    SandboxTemplateSpecVolumeClaimTemplatesPolicy,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class SandboxTemplate(_SandboxTemplate):
    """`pod_template` is the CRD's own only required field. `None` leaves an optional field
    unset, so the agent-sandbox controller's own default applies (`network_policy_management`
    defaults to `Managed`; `env_vars_injection_policy` and `volume_claim_templates_policy`
    default to `Disallowed`).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        pod_template: SandboxTemplateSpecPodTemplate,
        network_policy_management: SandboxTemplateSpecNetworkPolicyManagement | None = None,
        network_policy: SandboxTemplateSpecNetworkPolicy | None = None,
        env_vars_injection_policy: SandboxTemplateSpecEnvVarsInjectionPolicy | None = None,
        volume_claim_templates: Sequence[SandboxTemplateSpecVolumeClaimTemplates] = (),
        volume_claim_templates_policy: SandboxTemplateSpecVolumeClaimTemplatesPolicy | None = None,
        service: bool | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=SandboxTemplateSpec(
                pod_template=pod_template,
                network_policy_management=network_policy_management,
                network_policy=network_policy,
                env_vars_injection_policy=env_vars_injection_policy,
                volume_claim_templates=list(volume_claim_templates) or None,
                volume_claim_templates_policy=volume_claim_templates_policy,
                service=service,
            ),
        )
