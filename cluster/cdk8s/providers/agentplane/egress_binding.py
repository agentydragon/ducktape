"""Ergonomic wrapper for Agentplane's own `EgressBinding` CRD
(agentplane/crds/manifests/crd-egressbindings.yaml): a class named after the kind, following
cdk8s-plus's own construction pattern. Every keyword is an `EgressBindingSpec` field under its own
name and type; this wrapper adds no ducktape-specific policy.
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
    """Agentplane's `EgressBinding`: what sandboxes running as `subjects` may reach, as the union of
    the named `EgressPolicy` objects. `subjects` and `policies` are the fields the CRD itself
    requires."""

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
