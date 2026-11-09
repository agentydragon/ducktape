"""A Kubernetes account an environment's agents act as through the egress proxy, by name only.

`kubernetes_accounts.KubernetesAccount` renders the account and its use set; launch defaults, presets
and Flux bindings name the use set from here.
"""

from __future__ import annotations

from dataclasses import dataclass

from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace


@dataclass(frozen=True)
class KubernetesAccountRef:
    """A ServiceAccount in the accounts namespace."""

    namespace: str
    name: str

    @property
    def credential_name(self) -> str:
        """The name of the account's `EgressCredential` and of the one-rule `PolicySet` using it."""
        return f"k8s-account-{self.name}"


def account(namespace: str, name: str) -> KubernetesAccountRef:
    """The account `name` that agents of the environment in `namespace` act as."""
    return KubernetesAccountRef(namespace=kubernetes_accounts_namespace(namespace), name=name)


def shared_diagnostics(namespace: str) -> KubernetesAccountRef:
    """The read-only account every launch in the environment in `namespace` may act as."""
    return account(namespace, "shared-diagnostics")
