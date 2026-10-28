"""Deployment-side Kubernetes access profiles shared by static bindings and Agentplane.

Existing role owners still define rules and own their bindings. These are references and
selections, not a new runtime API. Namespace diagnostics come from namespace_access;
sensitive grants are explicit profile selections rather than diagnostics.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySetSpecRules,
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
    PolicySetSpecRulesRoleKind,
)
from cdk8s_plus_34 import Group, ISubject, ServiceAccount, k8s
from constructs import Construct

from agentplane.sandboxes.kubernetes_grants import (
    ClusterRoleBindingGrant,
    ClusterRoleRef,
    KubernetesGrant,
    RoleBindingGrant,
    RoleRef,
)
from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS, AgentReadable
from cluster.cdk8s.plaid_mcp import db, pgweb as plaid_pgweb, spend
from cluster.cdk8s.providers.agentplane.policy_set import Rule

RBAC_GROUP = "rbac.authorization.k8s.io"
BUILDBUDDY_KEY_POLICY = "buildbuddy-key"
BUILDBUDDY_KEY_ROLE = "buildbuddy-key-reader"
COINBASE_POLICY = "coinbase"
PLAID_DIAGNOSTICS_POLICY = "plaid-diagnostics"


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


def _namespace_read_grants() -> dict[str, RoleBindingGrant]:
    grants: dict[str, RoleBindingGrant] = {}
    # Keep staging first to preserve existing preset order.
    for namespace in ("agentplane-staging", *sorted(NAMESPACE_DIAGNOSTICS.keys() - {"agentplane-staging"})):
        grants[f"{namespace}-metadata"] = RoleBindingGrant(
            kind="RoleBinding",
            namespace=namespace,
            role_ref=RoleRef(kind="ClusterRole", name="agent-readable-namespace-metadata"),
        )
        if NAMESPACE_DIAGNOSTICS[namespace] is AgentReadable.LOGS:
            grants[f"{namespace}-logs"] = RoleBindingGrant(
                kind="RoleBinding",
                namespace=namespace,
                role_ref=RoleRef(kind="ClusterRole", name="agent-readable-namespace-logs"),
            )
    return grants


def catalog() -> dict[str, KubernetesGrant]:
    """Fresh existing grant models; consumers must not mutate another profile's catalog."""
    return {
        # Reuse public-coder's narrow cluster inventory, not Haku's broader
        # cluster-diagnostics-reader.
        "public-coder-node-read": ClusterRoleBindingGrant(
            kind="ClusterRoleBinding",
            role_ref=ClusterRoleRef(kind="ClusterRole", name="public-coder-agent-node-reader"),
        ),
        "public-coder-cluster-metadata-read": ClusterRoleBindingGrant(
            kind="ClusterRoleBinding",
            role_ref=ClusterRoleRef(kind="ClusterRole", name="public-coder-agent-cluster-metadata-reader"),
        ),
        "cluster-diagnostics": ClusterRoleBindingGrant(
            kind="ClusterRoleBinding", role_ref=ClusterRoleRef(kind="ClusterRole", name="cluster-diagnostics-reader")
        ),
        "haku-sandbox-write": RoleBindingGrant(
            kind="RoleBinding", namespace="haku-sandbox", role_ref=RoleRef(kind="Role", name="haku-sandbox-admin")
        ),
        BUILDBUDDY_KEY_POLICY: RoleBindingGrant(
            kind="RoleBinding", namespace="agentplane-staging", role_ref=RoleRef(kind="Role", name=BUILDBUDDY_KEY_ROLE)
        ),
        **_namespace_read_grants(),
        PLAID_DIAGNOSTICS_POLICY: RoleBindingGrant(
            kind="RoleBinding",
            namespace=db.NAMESPACE,
            role_ref=RoleRef(kind="Role", name=db.FINANCE_DIAGNOSTICS_READER),
        ),
        "spend-policy-config": RoleBindingGrant(
            kind="RoleBinding",
            namespace=spend.NAMESPACE,
            role_ref=RoleRef(kind="Role", name=spend.FINANCE_CONFIG_READER),
        ),
        "coinbase-credentials": RoleBindingGrant(
            kind="RoleBinding",
            namespace="agentplane-staging",
            role_ref=RoleRef(kind="Role", name="claude-ai-coinbase-reader"),
        ),
        "agentplane-sandbox-read": RoleBindingGrant(
            kind="RoleBinding",
            namespace="agentplane-staging",
            role_ref=RoleRef(kind="Role", name="claude-ai-sandbox-reader"),
        ),
        "haku-console-metadata": RoleBindingGrant(
            kind="RoleBinding",
            namespace="haku-console",
            role_ref=RoleRef(kind="Role", name="agent-haku-console-metadata-reader"),
        ),
        "clickhouse-diagnostics": RoleBindingGrant(
            kind="RoleBinding",
            namespace="clickhouse",
            role_ref=RoleRef(kind="Role", name="agent-clickhouse-diagnostics-reader"),
        ),
        "ducktape-flux-read": RoleBindingGrant(
            kind="RoleBinding", namespace="ducktape-flux", role_ref=RoleRef(kind="Role", name="ducktape-flux-reader")
        ),
        "public-coder-volsync-status": RoleBindingGrant(
            kind="RoleBinding",
            namespace="public-coder-agent",
            role_ref=RoleRef(kind="Role", name="agent-public-coder-extended-diagnostics-reader"),
        ),
        "public-coder-agent-reader": RoleBindingGrant(
            kind="RoleBinding",
            namespace="public-coder-agent",
            role_ref=RoleRef(kind="Role", name="public-coder-agent-reader"),
        ),
    }


SHARED_DIAGNOSTICS = (
    *_namespace_read_grants(),
    "haku-console-metadata",
    "clickhouse-diagnostics",
    "ducktape-flux-read",
    "public-coder-volsync-status",
    "public-coder-agent-reader",
)
PUBLIC_INVENTORY = ("public-coder-node-read", "public-coder-cluster-metadata-read")
HAKU_EXTRAS = ("cluster-diagnostics", "haku-sandbox-write")
PUBLIC_GRANTS = (*SHARED_DIAGNOSTICS, *PUBLIC_INVENTORY, "cluster-diagnostics")
# Shared grant membership for PolicySets. Each deployment chart composes these Kubernetes rules with
# its own rules before materializing the complete PolicySet, then separately materializes controller
# RBAC from the resolved assignable grants.
POLICY_SETS = {
    "shared-diagnostics": SHARED_DIAGNOSTICS,
    BUILDBUDDY_KEY_POLICY: (BUILDBUDDY_KEY_POLICY,),
    "public-inventory": PUBLIC_INVENTORY,
    "cluster-diagnostics": ("cluster-diagnostics",),
    "haku-sandbox-write": ("haku-sandbox-write",),
    COINBASE_POLICY: ("coinbase-credentials",),
    "spend-policy-config": ("spend-policy-config",),
    PLAID_DIAGNOSTICS_POLICY: (PLAID_DIAGNOSTICS_POLICY,),
}


def policy_set_grants(catalog: Mapping[str, KubernetesGrant]) -> dict[str, tuple[KubernetesGrant, ...]]:
    """Resolve the Kubernetes grants held by each shared, assignable PolicySet."""
    return {name: tuple(catalog[grant] for grant in grants) for name, grants in POLICY_SETS.items()}


def rules_for_grants(grants: Sequence[KubernetesGrant]) -> list[PolicySetSpecRules]:
    """Render Kubernetes grants as the rules a PolicySet materializes."""
    role_kinds = {"Role": PolicySetSpecRulesRoleKind.ROLE, "ClusterRole": PolicySetSpecRulesRoleKind.CLUSTER_ROLE}
    namespaces: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    cluster_roles: set[str] = set()
    for grant in grants:
        if isinstance(grant, RoleBindingGrant):
            namespaces[(grant.role_ref.kind, grant.role_ref.name)].add(grant.namespace)
        else:
            cluster_roles.add(grant.role_ref.name)
    return [
        *(
            Rule.kubernetes_role_binding(kind=role_kinds[kind], name=name, namespaces=sorted(namespaces[(kind, name)]))
            for kind, name in sorted(namespaces)
        ),
        *(Rule.kubernetes_cluster_role_binding(cluster_role=name) for name in sorted(cluster_roles)),
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


def covering_policy_sets(grants: Sequence[str]) -> list[str]:
    """The POLICY_SETS whose grants together are exactly `grants`, in POLICY_SETS order."""
    covering = [name for name, members in POLICY_SETS.items() if set(members) <= set(grants)]
    if uncovered := set(grants) - {grant for name in covering for grant in POLICY_SETS[name]}:
        raise ValueError(f"no PolicySet holds only launch grants among {sorted(grants)}: {sorted(uncovered)}")
    return covering


MANAGED_GRANTS = {
    "public-coder": PUBLIC_GRANTS,
    "finance-agent": (*PUBLIC_GRANTS, "coinbase-credentials", "spend-policy-config", PLAID_DIAGNOSTICS_POLICY),
    "haku": (*SHARED_DIAGNOSTICS, *HAKU_EXTRAS, "coinbase-credentials", PLAID_DIAGNOSTICS_POLICY),
}
STATIC_GRANTS = {
    # Independently scoped legacy OAuth identity, not an alias for the Haku profile. Widening it
    # fails cluster/validation's test_legacy_agentplane_accounts_are_not_haku_profile_aliases.
    "claude-ai": (
        "agentplane-sandbox-read",
        "haku-console-metadata",
        "clickhouse-diagnostics",
        "public-coder-agent-reader",
        "public-coder-volsync-status",
    ),
    # Preserve redundant narrow inventory bindings; Coinbase access is explicitly approved.
    "haku": (*SHARED_DIAGNOSTICS, *PUBLIC_INVENTORY, *HAKU_EXTRAS, "coinbase-credentials", BUILDBUDDY_KEY_POLICY),
}


def profile_subjects(grant: str) -> tuple[Subject, ...]:
    """Static profile membership for a binding, in its existing manifest order."""
    assert grant in catalog(), grant
    return tuple(
        subject
        for profile, identities in STATIC_IDENTITIES.items()
        if grant in STATIC_GRANTS[profile]
        for subject in identities
    )


def role_ref(grant: str) -> k8s.RoleRef:
    ref = catalog()[grant].role_ref
    return k8s.RoleRef(api_group=RBAC_GROUP, kind=ref.kind, name=ref.name)


def cleanup_namespaces() -> list[str]:
    # Retained cleanup scopes are intentionally independent of current selections.
    return sorted(
        {"haku-sandbox", "haku-console", "ducktape-flux", *(NAMESPACE_DIAGNOSTICS.keys() - {"agentplane-staging"})}
    )
