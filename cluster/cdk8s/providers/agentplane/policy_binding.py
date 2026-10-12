"""Ergonomic wrapper for Agentplane's own `PolicyBinding` CRD
(agentplane/crds/manifests/crd-policybindings.yaml), following cdk8s-plus's own construction
pattern: a class named after the kind.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_policybinding_crds.works.allegedly.agentplane import (
    PolicyBinding as _PolicyBinding,
    PolicyBindingSpec,
    PolicyBindingSpecSubject,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class PolicyBinding(_PolicyBinding):
    """Agentplane's `PolicyBinding`: grants `subject` every rule of the named `PolicySet`s in the
    binding's namespace."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        subject: PolicyBindingSpecSubject,
        policy_sets: Sequence[str],
    ) -> None:
        super().__init__(
            scope, id, metadata=metadata, spec=PolicyBindingSpec(subject=subject, policy_sets=list(policy_sets))
        )
