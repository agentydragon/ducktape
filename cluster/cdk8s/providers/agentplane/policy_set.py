"""Ergonomic wrapper for Agentplane's own `PolicySet` CRD
(agentplane/crds/manifests/crd-policysets.yaml), following cdk8s-plus's own construction
pattern: a class named after the kind, and a `Rule` factory group for the rule types' real
variant shapes.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySet as _PolicySet,
    PolicySetSpec,
    PolicySetSpecRules,
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
    PolicySetSpecRulesPolicy,
    PolicySetSpecRulesRole,
    PolicySetSpecRulesRoleKind,
    PolicySetSpecRulesType,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class Rule:
    """`PolicySetSpecRules`'s variant shapes. The schema's `oneOf` admits only the fields each
    `type` names; build through here rather than the raw generated struct."""

    @staticmethod
    def kubernetes_role_binding(
        *, kind: PolicySetSpecRulesRoleKind, name: str, namespaces: Sequence[str]
    ) -> PolicySetSpecRules:
        """Binds the existing Role or ClusterRole `name` in each of `namespaces`."""
        return PolicySetSpecRules(
            type=PolicySetSpecRulesType.KUBERNETES_UNDERSCORE_ROLE_UNDERSCORE_BINDING,
            role=PolicySetSpecRulesRole(kind=kind, name=name),
            namespaces=list(namespaces),
        )

    @staticmethod
    def kubernetes_cluster_role_binding(*, cluster_role: str) -> PolicySetSpecRules:
        """Binds the existing ClusterRole `cluster_role` cluster-wide."""
        return PolicySetSpecRules(
            type=PolicySetSpecRulesType.KUBERNETES_UNDERSCORE_CLUSTER_UNDERSCORE_ROLE_UNDERSCORE_BINDING,
            cluster_role=cluster_role,
        )

    @staticmethod
    def egress(
        *,
        hosts: Sequence[str],
        methods: Sequence[PolicySetSpecRulesMethods] | None = None,
        paths: Sequence[str] | None = None,
        cluster_internal: bool | None = None,
        credential_ref: PolicySetSpecRulesCredentialRef | None = None,
    ) -> PolicySetSpecRules:
        """Lets the subject reach `hosts` through the egress proxy; the other fields narrow it, as
        the CRD's field descriptions say."""
        return PolicySetSpecRules(
            type=PolicySetSpecRulesType.EGRESS,
            hosts=list(hosts),
            methods=None if methods is None else list(methods),
            paths=None if paths is None else list(paths),
            cluster_internal=cluster_internal,
            credential_ref=credential_ref,
        )

    @staticmethod
    def action_auto_approve(*, policy: PolicySetSpecRulesPolicy) -> PolicySetSpecRules:
        """Has the Action Service auto-approve a request `policy` matches."""
        return PolicySetSpecRules(type=PolicySetSpecRulesType.ACTION_UNDERSCORE_AUTO_UNDERSCORE_APPROVE, policy=policy)


class PolicySet(_PolicySet):
    """Agentplane's `PolicySet`: a reusable, subject-free set of typed rules. Build `rules` with
    `Rule`'s factories."""

    def __init__(
        self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, rules: Sequence[PolicySetSpecRules]
    ) -> None:
        super().__init__(scope, id, metadata=metadata, spec=PolicySetSpec(rules=list(rules)))
