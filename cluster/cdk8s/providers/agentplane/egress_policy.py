"""Ergonomic wrapper for Agentplane's own `EgressPolicy` CRD
(cluster/k8s/agentplane-crds/crd-egresspolicies.yaml): a class named after the kind, following
cdk8s-plus's own construction pattern.

The schema has exactly one shape for a rule: `hosts` plus ordinary optional fields
(`methods`, `paths`, `clusterInternal`, `credentialRef`) that narrow it, never alternative
typed structures -- so `rules` is a plain passthrough with no factory group.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicy as _EgressPolicy,
    EgressPolicySpec,
    EgressPolicySpecRules,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class EgressPolicy(_EgressPolicy):
    """Agentplane's `EgressPolicy`: a reusable, subject-free set of alternative rules (a request
    matching any one of `rules` is allowed). See the CRD's own field descriptions for the
    security contract of `clusterInternal` and `credentialRef`; this wrapper adds no
    ducktape-specific policy.
    """

    def __init__(
        self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, rules: Sequence[EgressPolicySpecRules]
    ) -> None:
        super().__init__(scope, id, metadata=metadata, spec=EgressPolicySpec(rules=list(rules)))
