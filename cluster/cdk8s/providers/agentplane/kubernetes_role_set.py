"""Typed factories for Agentplane's KubernetesRoleSet rule variants."""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_kubernetesroleset_crds.works.allegedly.agentplane import (
    KubernetesRoleSetSpecRules,
    KubernetesRoleSetSpecRulesRole,
    KubernetesRoleSetSpecRulesRoleKind,
    KubernetesRoleSetSpecRulesType,
)


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
