"""Deployment-side Kubernetes access profiles shared by static bindings and Agentplane.

Existing role owners still define rules and own their bindings. These are references and
selections, not a new runtime API. Namespace diagnostics come from namespace_access;
sensitive grants are explicit profile selections rather than diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cdk8s_plus_34 import Group, ISubject, ServiceAccount, k8s
from constructs import Construct

from agentplane.app.kubernetes_grants import (
    ClusterRoleBindingGrant,
    ClusterRoleRef,
    KubernetesGrant,
    RoleBindingGrant,
    RoleRef,
)
from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS, AgentReadable

RBAC_GROUP = "rbac.authorization.k8s.io"


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

    def json(self) -> dict[str, str]:
        if self.kind == "Group":
            return {"kind": self.kind, "name": self.name, "apiGroup": RBAC_GROUP}
        assert self.namespace is not None
        return {"kind": self.kind, "name": self.name, "namespace": self.namespace}

    def imported(self, scope: Construct, id: str) -> ISubject:
        if self.kind == "Group":
            return Group.from_name(scope, id, self.name)
        assert self.namespace is not None
        return ServiceAccount.from_service_account_name(scope, id, self.name, namespace_name=self.namespace)


HAKU_OIDC = Subject("Group", "oidc-ksbx-groups:haku")
HAKU_CONSOLE = Subject("Group", "haku:access-profile:haku")
HAKU_SERVICE_ACCOUNT = Subject("ServiceAccount", "haku", "haku-sandbox")
PUBLIC_CODER = Subject("Group", "haku:access-profile:public-coder")
CLAUDE_AI = Subject("ServiceAccount", "claude-ai", "agentplane-staging")
KUBECTL_USERS = Subject("Group", "oidc-ksbx-groups:kubectl-sandbox-users")
AGENT_BOX = Subject("Group", "oidc-ksbx-groups:agent-box-codex")
HAKU_IDENTITIES = (HAKU_OIDC, HAKU_CONSOLE, HAKU_SERVICE_ACCOUNT)
STATIC_IDENTITIES = {"haku": HAKU_IDENTITIES, "public-coder": (PUBLIC_CODER,)}
# These legacy subjects are deliberately not aliases for the Haku profile.
NAMESPACE_READER_SUBJECTS = (*HAKU_IDENTITIES, KUBECTL_USERS, PUBLIC_CODER, CLAUDE_AI)
CLUSTER_DIAGNOSTIC_SUBJECTS = (KUBECTL_USERS, *HAKU_IDENTITIES, AGENT_BOX, CLAUDE_AI)
TESTING_OPERATOR_SUBJECTS = (HAKU_OIDC, HAKU_CONSOLE, PUBLIC_CODER, HAKU_SERVICE_ACCOUNT, KUBECTL_USERS)


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
        # cluster-diagnostics-reader (which includes node proxy access).
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
        "agentplane-testing-operator": RoleBindingGrant(
            kind="RoleBinding",
            namespace="agentplane-testing",
            role_ref=RoleRef(kind="Role", name="agentplane-testing-operator"),
        ),
        **_namespace_read_grants(),
        "coinbase-credentials": RoleBindingGrant(
            kind="RoleBinding",
            namespace="agentplane-staging",
            role_ref=RoleRef(kind="Role", name="claude-ai-coinbase-reader"),
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
        "agentplane-testing-login": RoleBindingGrant(
            kind="RoleBinding",
            namespace="public-coder-agent",
            role_ref=RoleRef(kind="Role", name="agentplane-testing-login-reader"),
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
TESTING_ACCESS = ("agentplane-testing-operator", "agentplane-testing-login")
PUBLIC_INVENTORY = ("public-coder-node-read", "public-coder-cluster-metadata-read")
HAKU_EXTRAS = ("cluster-diagnostics", "haku-sandbox-write")
PUBLIC_GRANTS = (*SHARED_DIAGNOSTICS, *TESTING_ACCESS, *PUBLIC_INVENTORY)
MANAGED_GRANTS = {
    "public-coder": PUBLIC_GRANTS,
    "finance-agent": (*PUBLIC_GRANTS, "coinbase-credentials"),
    "haku": (*SHARED_DIAGNOSTICS, *TESTING_ACCESS, *HAKU_EXTRAS, "coinbase-credentials"),
}
STATIC_GRANTS = {
    "public-coder": PUBLIC_GRANTS,
    # Preserve redundant narrow inventory bindings; Coinbase access is explicitly approved.
    "haku": (*SHARED_DIAGNOSTICS, *TESTING_ACCESS, *PUBLIC_INVENTORY, *HAKU_EXTRAS, "coinbase-credentials"),
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
