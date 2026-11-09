"""A Kubernetes account an environment's agents act as through the egress proxy, by name only.

`kubernetes_accounts.KubernetesAccount` renders the account; the egress proxy's `basic` set names its
credential. Both read the names from here, so neither imports the other.
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


def shared_diagnostics(namespace: str) -> KubernetesAccountRef:
    """The read-only account the `basic` set lets every agent of the environment in `namespace` act as."""
    return KubernetesAccountRef(namespace=kubernetes_accounts_namespace(namespace), name="shared-diagnostics")
