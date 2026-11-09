"""The namespace of the Kubernetes accounts the egress proxy acts as, and its right to act as them.

A `kubernetesServiceAccount` EgressCredential has the proxy present its own bearer with
`Impersonate-User` naming an account. The Role here is the whole of that right: `impersonate` on
ServiceAccounts in this one namespace, so no other account, user or group is reachable through it.
The accounts themselves and their grants are rendered by their owners, not here.
"""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, ServiceAccount
from constructs import Construct

from cluster.cdk8s import namespaces
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.namespaces import Vpa


class KubernetesAccounts(Construct):
    """The accounts namespace and the proxy's impersonation Role in it."""

    def __init__(self, scope: Construct, id: str, *, namespace: str, proxy_namespace: str) -> None:
        super().__init__(scope, id)
        namespaces.namespace(
            self,
            "namespace",
            name=namespace,
            # ServiceAccounts only: nothing runs here.
            vpa=Vpa.DISABLED,
            annotations={
                "description": f"Kubernetes accounts the {proxy_namespace} egress proxy acts as by impersonation."
            },
        )
        role = Role(
            self,
            "impersonator",
            metadata=ApiObjectMetadata(name="egress-impersonator", namespace=namespace),
            rules=[RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["impersonate"])],
        )
        RoleBinding(
            self, "impersonator-binding", metadata=ApiObjectMetadata(name=egress.NAME, namespace=namespace), role=role
        ).add_subjects(
            ServiceAccount.from_service_account_name(self, "proxy", egress.NAME, namespace_name=proxy_namespace)
        )
