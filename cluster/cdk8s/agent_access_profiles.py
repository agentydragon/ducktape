"""Deployment-side Kubernetes access profiles shared by static bindings and Agentplane.

Existing role owners still define rules and own their bindings. These are references and
selections, not a new runtime API. Namespace diagnostics come from namespace_access;
sensitive grants are explicit profile selections rather than diagnostics.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from agentplane_kubernetesroleset_crds.works.allegedly.agentplane import (
    KubernetesRoleSetSpecRules,
    KubernetesRoleSetSpecRulesRoleKind,
)
from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySetSpecRules,
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
    PolicySetSpecRulesRoleKind,
)
from cdk8s_plus_34 import Group, ISubject, ServiceAccount, k8s
from constructs import Construct

from cluster.cdk8s.agentplane.kubernetes_grants import (
    ClusterRoleBindingGrant,
    ClusterRoleRef,
    KubernetesGrant,
    RoleBindingGrant,
    RoleRef,
)
from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS
from cluster.cdk8s.plaid_mcp import db, pgweb as plaid_pgweb, spend
from cluster.cdk8s.providers.agentplane.kubernetes_role_set import KubernetesRule
from cluster.cdk8s.providers.agentplane.policy_set import Rule

RBAC_GROUP = "rbac.authorization.k8s.io"
COINBASE_POLICY = "coinbase"
OVH_BILLING_POLICY = "ovh-billing"
PLAID_DIAGNOSTICS_POLICY = "plaid-diagnostics"
SHARED_DIAGNOSTICS_POLICY = "shared-diagnostics"
CLUSTER_DIAGNOSTICS_POLICY = "cluster-diagnostics"
CLAUDE_AI_STATIC_ROLE_SET = "claude-ai-static"


@dataclass(frozen=True)
class Subject:
    """One identity, not a union of all the identities used by the same agent."""

    kind: Literal["Group", "ServiceAccount"]
    name: str
    namespace: str | None = None

    def k8s(self) -> k8s.Subject:
        return k8s.Subject(
            kind=self.kind,
            name=self.name,
            namespace=self.namespace,
            api_group=RBAC_GROUP if self.kind == "Group" else None,
        )

    def imported(self, scope: Construct, id: str) -> ISubject:
        if self.kind == "Group":
            return Group.from_name(scope, id, self.name)
        assert self.namespace is not None
        return ServiceAccount.from_service_account_name(scope, id, self.name, namespace_name=self.namespace)


HAKU_OIDC = Subject("Group", "oidc-ksbx-groups:haku")
HAKU_CONSOLE = Subject("Group", "haku:access-profile:haku")
HAKU_SERVICE_ACCOUNT = Subject("ServiceAccount", "haku", "haku-sandbox")
CLAUDE_AI = Subject("ServiceAccount", "claude-ai", "agentplane-staging")
KUBECTL_USERS = Subject("Group", "oidc-ksbx-groups:kubectl-sandbox-users")
HAKU_IDENTITIES = (HAKU_OIDC, HAKU_CONSOLE, HAKU_SERVICE_ACCOUNT)
STATIC_IDENTITIES = {"haku": HAKU_IDENTITIES, "claude-ai": (CLAUDE_AI,)}
# These legacy subjects are deliberately not aliases for the Haku profile.
NAMESPACE_READER_SUBJECTS = (*HAKU_IDENTITIES, KUBECTL_USERS, CLAUDE_AI)
CLUSTER_DIAGNOSTIC_SUBJECTS = (KUBECTL_USERS, *HAKU_IDENTITIES, CLAUDE_AI)


# Keep staging first to preserve the deployed order.
_NAMESPACE_READ_GRANTS = {
    f"{namespace}-{level}": RoleBindingGrant(
        namespace=namespace, role_ref=RoleRef(kind="ClusterRole", name=f"agent-readable-namespace-{level}")
    )
    for namespace in ("agentplane-staging", *sorted(NAMESPACE_DIAGNOSTICS.keys() - {"agentplane-staging"}))
    for level in NAMESPACE_DIAGNOSTICS[namespace].levels
}

_GRANTS: dict[str, KubernetesGrant] = {
    # Reuse public-coder's narrow cluster inventory, not Haku's broader
    # cluster-diagnostics-reader.
    "public-coder-node-read": ClusterRoleBindingGrant(
        role_ref=ClusterRoleRef(kind="ClusterRole", name="public-coder-agent-node-reader")
    ),
    "public-coder-cluster-metadata-read": ClusterRoleBindingGrant(
        role_ref=ClusterRoleRef(kind="ClusterRole", name="public-coder-agent-cluster-metadata-reader")
    ),
    "cluster-diagnostics": ClusterRoleBindingGrant(
        role_ref=ClusterRoleRef(kind="ClusterRole", name="cluster-diagnostics-reader")
    ),
    "haku-sandbox-write": RoleBindingGrant(
        namespace="haku-sandbox", role_ref=RoleRef(kind="Role", name="haku-sandbox-admin")
    ),
    **_NAMESPACE_READ_GRANTS,
    PLAID_DIAGNOSTICS_POLICY: RoleBindingGrant(
        namespace=db.NAMESPACE, role_ref=RoleRef(kind="Role", name=db.FINANCE_DIAGNOSTICS_READER)
    ),
    "spend-policy-config": RoleBindingGrant(
        namespace=spend.NAMESPACE, role_ref=RoleRef(kind="Role", name=spend.FINANCE_CONFIG_READER)
    ),
    "ovh-billing-credentials": RoleBindingGrant(
        namespace="agentplane-staging", role_ref=RoleRef(kind="Role", name="ovh-billing-reader")
    ),
    "coinbase-credentials": RoleBindingGrant(
        namespace="agentplane-staging", role_ref=RoleRef(kind="Role", name="claude-ai-coinbase-reader")
    ),
    "agentplane-sandbox-read": RoleBindingGrant(
        namespace="agentplane-staging", role_ref=RoleRef(kind="Role", name="claude-ai-sandbox-reader")
    ),
    "haku-console-metadata": RoleBindingGrant(
        namespace="haku-console", role_ref=RoleRef(kind="Role", name="agent-haku-console-metadata-reader")
    ),
    "clickhouse-diagnostics": RoleBindingGrant(
        namespace="clickhouse", role_ref=RoleRef(kind="Role", name="agent-clickhouse-diagnostics-reader")
    ),
    "ducktape-flux-read": RoleBindingGrant(
        namespace="ducktape-flux", role_ref=RoleRef(kind="Role", name="ducktape-flux-reader")
    ),
    "public-coder-volsync-status": RoleBindingGrant(
        namespace="public-coder-agent",
        role_ref=RoleRef(kind="Role", name="agent-public-coder-extended-diagnostics-reader"),
    ),
    "public-coder-agent-reader": RoleBindingGrant(
        namespace="public-coder-agent", role_ref=RoleRef(kind="Role", name="public-coder-agent-reader")
    ),
}


SHARED_DIAGNOSTICS = (
    *_NAMESPACE_READ_GRANTS,
    "haku-console-metadata",
    "clickhouse-diagnostics",
    "ducktape-flux-read",
    "public-coder-volsync-status",
    "public-coder-agent-reader",
)
PUBLIC_INVENTORY = ("public-coder-node-read", "public-coder-cluster-metadata-read")
HAKU_EXTRAS = ("cluster-diagnostics", "haku-sandbox-write")
# Shared grant membership for PolicySets. Each deployment chart composes these Kubernetes rules with
# its own rules before materializing the complete PolicySet, then separately materializes controller
# RBAC from the resolved assignable grants.
POLICY_SETS = {
    SHARED_DIAGNOSTICS_POLICY: SHARED_DIAGNOSTICS,
    "public-inventory": PUBLIC_INVENTORY,
    CLUSTER_DIAGNOSTICS_POLICY: ("cluster-diagnostics",),
    "haku-sandbox-write": ("haku-sandbox-write",),
    COINBASE_POLICY: ("coinbase-credentials",),
    OVH_BILLING_POLICY: ("ovh-billing-credentials",),
    "spend-policy-config": ("spend-policy-config",),
    PLAID_DIAGNOSTICS_POLICY: (PLAID_DIAGNOSTICS_POLICY,),
}


def policy_set_grants() -> dict[str, tuple[KubernetesGrant, ...]]:
    """Resolve the Kubernetes grants held by each shared, assignable PolicySet."""
    return {name: tuple(_GRANTS[grant] for grant in grants) for name, grants in POLICY_SETS.items()}


def _bindings_for_grants(grants: Sequence[KubernetesGrant]) -> tuple[list[tuple[str, str, list[str]]], list[str]]:
    """The grants as (role kind, role name, namespaces) RoleBinding groups and bound ClusterRoles."""
    namespaces: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    cluster_roles: set[str] = set()
    for grant in grants:
        if isinstance(grant, RoleBindingGrant):
            namespaces[(grant.role_ref.kind, grant.role_ref.name)].add(grant.namespace)
        else:
            cluster_roles.add(grant.role_ref.name)
    return [(kind, name, sorted(namespaces[(kind, name)])) for kind, name in sorted(namespaces)], sorted(cluster_roles)


def rules_for_grants(grants: Sequence[KubernetesGrant]) -> list[PolicySetSpecRules]:
    """Render Kubernetes grants as the rules a PolicySet materializes."""
    role_kinds = {"Role": PolicySetSpecRulesRoleKind.ROLE, "ClusterRole": PolicySetSpecRulesRoleKind.CLUSTER_ROLE}
    role_bindings, cluster_roles = _bindings_for_grants(grants)
    return [
        *(
            Rule.kubernetes_role_binding(kind=role_kinds[kind], name=name, namespaces=namespaces)
            for kind, name, namespaces in role_bindings
        ),
        *(Rule.kubernetes_cluster_role_binding(cluster_role=name) for name in cluster_roles),
    ]


def role_set_rules_for_grants(grants: Sequence[KubernetesGrant]) -> list[KubernetesRoleSetSpecRules]:
    """Render Kubernetes grants as the rules a KubernetesRoleSet materializes."""
    role_kinds = {
        "Role": KubernetesRoleSetSpecRulesRoleKind.ROLE,
        "ClusterRole": KubernetesRoleSetSpecRulesRoleKind.CLUSTER_ROLE,
    }
    role_bindings, cluster_roles = _bindings_for_grants(grants)
    return [
        *(
            KubernetesRule.role_binding(kind=role_kinds[kind], name=name, namespaces=namespaces)
            for kind, name, namespaces in role_bindings
        ),
        *(KubernetesRule.cluster_role_binding(cluster_role=name) for name in cluster_roles),
    ]


def plaid_pgweb_egress_rules(*, credential: str) -> list[PolicySetSpecRules]:
    """The egress half of the Plaid diagnostics PolicySet."""
    host = plaid_pgweb.SERVICE.fqdn
    # pgweb's other API routes and its web UI stay unreachable. Its session routes (`connect`,
    # `disconnect`, `switchdb`) are POSTs, so POST is admitted on the query route alone.
    reads = [
        # keep-sorted start
        "/api/connection",
        "/api/info",
        "/api/objects",
        "/api/query",
        "/api/schemas",
        "/api/tables/**",
        # keep-sorted end
    ]

    def egress_rule(method: PolicySetSpecRulesMethods, paths: Sequence[str]) -> PolicySetSpecRules:
        return Rule.egress(
            hosts=[host],
            cluster_internal=True,
            methods=[method],
            paths=paths,
            credential_ref=PolicySetSpecRulesCredentialRef(name=credential),
        )

    return [
        egress_rule(PolicySetSpecRulesMethods.GET, reads),
        egress_rule(PolicySetSpecRulesMethods.POST, ["/api/query"]),
    ]


# The KubernetesRoleSets each launch account holds. Sets with egress rules also
# supply those rules to the preset's Identity in the selected catalog mode.
_PUBLIC_ROLE_SETS = (SHARED_DIAGNOSTICS_POLICY, "public-inventory", CLUSTER_DIAGNOSTICS_POLICY)
PRESET_ROLE_SETS = {
    "public-coder": _PUBLIC_ROLE_SETS,
    "finance-agent": (
        *_PUBLIC_ROLE_SETS,
        COINBASE_POLICY,
        OVH_BILLING_POLICY,
        "spend-policy-config",
        PLAID_DIAGNOSTICS_POLICY,
    ),
    "haku": (
        SHARED_DIAGNOSTICS_POLICY,
        CLUSTER_DIAGNOSTICS_POLICY,
        "haku-sandbox-write",
        COINBASE_POLICY,
        OVH_BILLING_POLICY,
        PLAID_DIAGNOSTICS_POLICY,
    ),
}
STATIC_GRANTS = {
    # Independently scoped legacy OAuth identity, not an alias for the Haku profile. Widening it
    # fails test_agent_rbac.py's test_legacy_agentplane_accounts_are_not_haku_profile_aliases.
    "claude-ai": (
        "agentplane-sandbox-read",
        "haku-console-metadata",
        "clickhouse-diagnostics",
        "public-coder-agent-reader",
        "public-coder-volsync-status",
    ),
    # Preserve redundant narrow inventory bindings; Coinbase access is explicitly approved.
    "haku": (*SHARED_DIAGNOSTICS, *PUBLIC_INVENTORY, *HAKU_EXTRAS, "coinbase-credentials", "ovh-billing-credentials"),
}


# Direct grants on the old Claude caller ServiceAccount. Its standing Identity account
# receives Coinbase and Plaid access separately through the same-named role sets.
CLAUDE_AI_STATIC_GRANTS = (
    _GRANTS[CLUSTER_DIAGNOSTICS_POLICY],
    *_NAMESPACE_READ_GRANTS.values(),
    *(_GRANTS[name] for name in STATIC_GRANTS["claude-ai"]),
)


def profile_subjects(grant: str) -> tuple[Subject, ...]:
    """Static profile membership for a binding, in its existing manifest order."""
    assert grant in _GRANTS, grant
    return tuple(
        subject
        for profile, identities in STATIC_IDENTITIES.items()
        if grant in STATIC_GRANTS[profile]
        for subject in identities
    )


def role_ref(grant: str) -> k8s.RoleRef:
    ref = _GRANTS[grant].role_ref
    return k8s.RoleRef(api_group=RBAC_GROUP, kind=ref.kind, name=ref.name)


def cleanup_namespaces() -> list[str]:
    # Retained cleanup scopes are intentionally independent of current selections.
    return sorted(
        {"haku-sandbox", "haku-console", "ducktape-flux", *(NAMESPACE_DIAGNOSTICS.keys() - {"agentplane-staging"})}
    )
