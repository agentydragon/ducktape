"""The namespace of the Kubernetes accounts the egress proxy acts as, and its right to act as them.

A `kubernetesServiceAccount` EgressCredential has the proxy present its own bearer with
`Impersonate-User` naming an account. The Role here is the whole of that right: `impersonate` on
ServiceAccounts in this one namespace, so no other account, user or group is reachable through it.
`KubernetesAccount` renders one static account with what makes it usable.
"""

from collections.abc import Sequence

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, ServiceAccount
from constructs import Construct

from agentplane.egress.resources import placeholder_of
from cluster.cdk8s import namespaces
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.agentplane.kubernetes_account_ref import KubernetesAccountRef
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.kubernetes_role_set import KubernetesRoleSet
from cluster.cdk8s.providers.agentplane.policy_set import PolicySet
from cluster.cdk8s.providers.agentplane.service_account_grant import ServiceAccountGrant


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


class KubernetesAccount(Construct):
    """A Flux-owned account: the ServiceAccount, its standing grant of `role_sets`, the credential the
    proxy impersonates it with, and the one-rule `PolicySet` a binding names to act as it. Every
    object but the ServiceAccount is in `policy_namespace`, beside the role sets."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        account: KubernetesAccountRef,
        policy_namespace: str,
        description: str,
        role_sets: Sequence[KubernetesRoleSet],
    ) -> None:
        super().__init__(scope, id)
        self.service_account = ServiceAccount(
            self,
            "service-account",
            metadata=ApiObjectMetadata(
                name=account.name, namespace=account.namespace, annotations={"description": description}
            ),
            # Nothing runs as it: the proxy impersonates it.
            automount_token=False,
        )
        self.grant = ServiceAccountGrant(
            self,
            "grant",
            metadata=ApiObjectMetadata(name=account.name, namespace=policy_namespace),
            service_account=self.service_account,
            role_sets=role_sets,
        )
        self.credential = EgressCredential(
            self,
            "credential",
            metadata=ApiObjectMetadata(name=account.credential_name, namespace=policy_namespace),
            description=(
                f"Kubernetes API server: https://{egress.KUBERNETES_HOST}, acting as the "
                f"{account.namespace}/{account.name} ServiceAccount: {description} Use "
                f"kubectl --server https://{egress.KUBERNETES_HOST} --token {placeholder_of(account.credential_name)} "
                "for Kubernetes commands. Check agentplane egress rules for the live host and placeholder."
            ),
            source=Source.kubernetes_service_account(service_account=self.service_account),
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
                )
            ],
        )
        self.use = PolicySet(
            self,
            "use",
            metadata=ApiObjectMetadata(name=account.credential_name, namespace=policy_namespace),
            rules=[egress.kubernetes_account_rule(account)],
        )
