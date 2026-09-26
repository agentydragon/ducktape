"""Ergonomic wrapper for Agentplane's own `EgressBinding` CRD
(cluster/k8s/agentplane-crds/crd-egressbindings.yaml): a class named after the kind, following
cdk8s-plus's own construction pattern. The schema has exactly one shape -- `subjects` plus
`policies` plus optional `expiresAt` -- so this is a plain passthrough with no factory group.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from agentplane_egressbinding_crds.works.allegedly.agentplane import (
    EgressBinding as _EgressBinding,
    EgressBindingSpec,
    EgressBindingSpecSubjects,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class EgressBinding(_EgressBinding):
    """Agentplane's `EgressBinding`: attaches `policies` (EgressPolicy names in the binding's
    namespace) to `subjects`. The binding's existence is the grant; `expires_at` unset (`None`)
    means it never expires.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        subjects: Sequence[EgressBindingSpecSubjects],
        policies: Sequence[str],
        expires_at: datetime.datetime | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=EgressBindingSpec(subjects=list(subjects), policies=list(policies), expires_at=expires_at),
        )
