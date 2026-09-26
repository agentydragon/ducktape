"""Ergonomic wrapper for Agentplane's own `ActionPolicyBinding` CRD
(cluster/k8s/agentplane-crds/crd-actionpolicybindings.yaml): a class named after the kind,
following cdk8s-plus's own construction pattern. The schema has exactly one shape -- `subject`
plus `policySets` plus optional `expiresAt` -- so this is a plain passthrough with no factory
group.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from agentplane_actionpolicybinding_crds.works.allegedly.agentplane import (
    ActionPolicyBinding as _ActionPolicyBinding,
    ActionPolicyBindingSpec,
    ActionPolicyBindingSpecSubject,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class ActionPolicyBinding(_ActionPolicyBinding):
    """Agentplane's `ActionPolicyBinding`: attaches `policy_sets` (ActionPolicySet names in the
    binding's namespace) to one `subject`. The binding's existence is the grant; `expires_at`
    unset (`None`) means it never expires.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        subject: ActionPolicyBindingSpecSubject,
        policy_sets: Sequence[str],
        expires_at: datetime.datetime | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ActionPolicyBindingSpec(subject=subject, policy_sets=list(policy_sets), expires_at=expires_at),
        )
