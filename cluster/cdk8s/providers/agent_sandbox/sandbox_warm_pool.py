"""Ergonomic wrapper for agent-sandbox's `SandboxWarmPool`, following cdk8s-plus's own
construction pattern: a class named after the kind, with keyword parameters mirroring
`SandboxWarmPoolSpec`'s own fields.
"""

from __future__ import annotations

from agent_sandbox_sandboxwarmpool_crds.io.x_k8s.agents.extensions import (
    SandboxWarmPool as _SandboxWarmPool,
    SandboxWarmPoolSpec,
    SandboxWarmPoolSpecSandboxTemplateRef,
    SandboxWarmPoolSpecUpdateStrategy,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class SandboxWarmPool(_SandboxWarmPool):
    """`sandbox_template_ref`, a `SandboxTemplate` in the pool's own namespace, is the CRD's only
    required field. `None` leaves an optional field unset, so the agent-sandbox controller's own
    default applies (`replicas` 1, `update_strategy` `OnReplenish`).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        sandbox_template_ref: SandboxWarmPoolSpecSandboxTemplateRef,
        replicas: int | None = None,
        update_strategy: SandboxWarmPoolSpecUpdateStrategy | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=SandboxWarmPoolSpec(
                sandbox_template_ref=sandbox_template_ref, replicas=replicas, update_strategy=update_strategy
            ),
        )
