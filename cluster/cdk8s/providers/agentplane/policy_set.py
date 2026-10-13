"""Ergonomic wrapper for Agentplane's own `PolicySet` CRD
(agentplane/crds/manifests/crd-policysets.yaml), following cdk8s-plus's own construction
pattern: a class named after the kind, and a `Rule` factory group for the rule types' real
variant shapes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySet as _PolicySet,
    PolicySetSpec,
    PolicySetSpecRules,
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
    PolicySetSpecRulesPolicy,
    PolicySetSpecRulesPolicyType,
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


class AutoApproveIf:
    """`action_auto_approve` rules by policy kind: `exact_actions` (an Action name allowlist),
    `github_repository` (a fixed owner/repository), `github_public_repository` (a live,
    unauthenticated visibility check in place of a fixed owner/repository), and `argument_schema`
    (arguments satisfying a JSON Schema). The schema also defines `home_assistant_entity_control`
    (a Home Assistant service call confined to configured entities and services) -- add a factory
    the day this repo builds one.
    """

    @staticmethod
    def exact_actions(*, actions: Mapping[str, Sequence[str]]) -> PolicySetSpecRules:
        """Matches by Action name alone."""
        return Rule.action_auto_approve(
            policy=PolicySetSpecRulesPolicy(type=PolicySetSpecRulesPolicyType.EXACT_UNDERSCORE_ACTIONS, actions=actions)
        )

    @staticmethod
    def github_repository(*, owner: str, repository: str, actions: Mapping[str, Sequence[str]]) -> PolicySetSpecRules:
        """Requires a GitHub MCP call to target `owner`/`repository`."""
        return Rule.action_auto_approve(
            policy=PolicySetSpecRulesPolicy(
                type=PolicySetSpecRulesPolicyType.GITHUB_UNDERSCORE_REPOSITORY,
                owner=owner,
                repository=repository,
                actions=actions,
            )
        )

    @staticmethod
    def argument_schema(*, actions: Mapping[str, Sequence[str]], schema: dict[str, Any]) -> PolicySetSpecRules:
        """Requires a listed Action's arguments to satisfy `schema`."""
        return Rule.action_auto_approve(
            policy=PolicySetSpecRulesPolicy(
                type=PolicySetSpecRulesPolicyType.ARGUMENT_UNDERSCORE_SCHEMA, actions=actions, schema=schema
            )
        )

    @staticmethod
    def github_public_repository(*, actions: Mapping[str, Sequence[str]]) -> PolicySetSpecRules:
        """Requires a GitHub MCP call to target a repository a live, unauthenticated lookup
        confirms public."""
        return Rule.action_auto_approve(
            policy=PolicySetSpecRulesPolicy(
                type=PolicySetSpecRulesPolicyType.GITHUB_UNDERSCORE_PUBLIC_UNDERSCORE_REPOSITORY, actions=actions
            )
        )


class PolicySet(_PolicySet):
    """Agentplane's `PolicySet`: a reusable, subject-free set of typed rules. Build `rules` with
    `Rule`'s factories."""

    def __init__(
        self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, rules: Sequence[PolicySetSpecRules]
    ) -> None:
        super().__init__(scope, id, metadata=metadata, spec=PolicySetSpec(rules=list(rules)))
