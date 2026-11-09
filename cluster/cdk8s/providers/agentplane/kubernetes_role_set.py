"""Ergonomic wrapper for Agentplane's own `KubernetesRoleSet` CRD
(agentplane/crds/manifests/crd-kubernetesrolesets.yaml), following cdk8s-plus's own construction
pattern: a class named after the kind, and a `KubernetesRule` factory group for the rule types'
variant shapes.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_kubernetesroleset_crds.works.allegedly.agentplane import (
    KubernetesRoleSet as _KubernetesRoleSet,
    KubernetesRoleSetSpec,
    KubernetesRoleSetSpecRules,
    KubernetesRoleSetSpecRulesRole,
    KubernetesRoleSetSpecRulesRoleKind,
    KubernetesRoleSetSpecRulesType,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class KubernetesRule:
    """`KubernetesRoleSetSpecRules`'s variant shapes; the schema admits only the fields each `type`
    names."""

    @staticmethod
    def role_binding(
        *, kind: KubernetesRoleSetSpecRulesRoleKind, name: str, namespaces: Sequence[str]
    ) -> KubernetesRoleSetSpecRules:
        """Binds the existing Role or ClusterRole `name` in each of `namespaces`."""
        return KubernetesRoleSetSpecRules(
            type=KubernetesRoleSetSpecRulesType.ROLE_UNDERSCORE_BINDING,
            role=KubernetesRoleSetSpecRulesRole(kind=kind, name=name),
            namespaces=list(namespaces),
        )

    @staticmethod
    def cluster_role_binding(*, cluster_role: str) -> KubernetesRoleSetSpecRules:
        """Binds the existing ClusterRole `cluster_role` cluster-wide."""
        return KubernetesRoleSetSpecRules(
            type=KubernetesRoleSetSpecRulesType.CLUSTER_UNDERSCORE_ROLE_UNDERSCORE_BINDING, cluster_role=cluster_role
        )


class KubernetesRoleSet(_KubernetesRoleSet):
    """Agentplane's `KubernetesRoleSet`: a reusable, subject-free set of native RBAC bindings a
    `ServiceAccountGrant` attaches to an account. Build `rules` with `KubernetesRule`'s factories."""

    def __init__(
        self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, rules: Sequence[KubernetesRoleSetSpecRules]
    ) -> None:
        super().__init__(scope, id, metadata=metadata, spec=KubernetesRoleSetSpec(rules=list(rules)))
