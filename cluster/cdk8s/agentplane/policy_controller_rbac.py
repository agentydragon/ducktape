"""The RBAC granted to the Agentplane Policy Controller for assignable Kubernetes grants."""

from __future__ import annotations

from collections.abc import Sequence, Set
from typing import cast

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ApiResource, IApiResource, Role, RoleBinding, RolePolicyRule, ServiceAccount, k8s
from constructs import Construct

from agentplane.sandboxes.kubernetes_grants import KubernetesGrant, RoleBindingGrant
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.managed_account_admission import add_managed_account_admission
from cluster.cdk8s.agentplane.policy_controller import NAME
from cluster.cdk8s.api_resource import custom_resource, named_resource

_RBAC = "rbac.authorization.k8s.io"
_GROUP = "agentplane.allegedly.works"


class PolicyControllerRbac(Construct):
    """Materialize the controller's fixed API access and bind authority for assignable grants."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        account: ServiceAccount,
        assignable_grants: Sequence[KubernetesGrant],
        accounts_namespace: str,
        workload_namespaces: Set[str],
    ) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        role_name = NAME.removeprefix("agentplane-")
        own_roles = sorted(
            {
                grant.role_ref.name
                for grant in assignable_grants
                if isinstance(grant, RoleBindingGrant)
                and grant.role_ref.kind == "Role"
                and grant.namespace == namespace
            }
        )
        cluster_roles = sorted(
            {grant.role_ref.name for grant in assignable_grants if grant.role_ref.kind == "ClusterRole"}
        )
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
            rules=[
                # A ManagedServiceAccount's EgressCredential and use PolicySet, which it owns.
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "policysets"), custom_resource(_GROUP, "egresscredentials")],
                    verbs=["create", "update", "patch", "get", "list", "watch"],
                ),
                RolePolicyRule(resources=[custom_resource(_GROUP, "sandboxes")], verbs=["get"]),
                # Deleting is how a binding expires.
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "policybindings")],
                    verbs=["create", "get", "list", "watch", "delete"],
                ),
                RolePolicyRule(resources=[custom_resource(_GROUP, "policybindings/status")], verbs=["patch"]),
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "kubernetesrolesets")], verbs=["get", "list", "watch"]
                ),
                # PolicyService serves identities and the operator's delete of one.
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "identities")], verbs=["get", "list", "watch", "delete"]
                ),
                # Deleting is how a grant or a managed account expires.
                RolePolicyRule(
                    resources=[
                        custom_resource(_GROUP, "serviceaccountgrants"),
                        custom_resource(_GROUP, "managedserviceaccounts"),
                    ],
                    verbs=["get", "list", "watch", "delete"],
                ),
                RolePolicyRule(
                    resources=[
                        custom_resource(_GROUP, "serviceaccountgrants/status"),
                        custom_resource(_GROUP, "managedserviceaccounts/status"),
                    ],
                    verbs=["patch"],
                ),
                RolePolicyRule(
                    resources=[cast(IApiResource, ApiResource.SERVICE_ACCOUNTS)], verbs=["get", "list", "watch"]
                ),
                # Leader election.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")], verbs=["get", "create", "update"]
                ),
                RolePolicyRule(resources=[custom_resource("", "events")], verbs=["create", "patch"]),
                # RoleBindings in its own namespace; binding_delegation.py grants the same elsewhere.
                RolePolicyRule(resources=[custom_resource(_RBAC, "rolebindings")], verbs=["create", "delete", "patch"]),
                *(
                    RolePolicyRule(resources=[named_resource(_RBAC, "roles", name)], verbs=["bind"])
                    for name in own_roles
                ),
            ],
        )
        RoleBinding(
            self,
            "role-binding",
            metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", role_name),
        ).add_subjects(account)
        # Narrows the EgressCredential and PolicySet writes above to managed accounts' use.
        add_managed_account_admission(
            self,
            namespace=namespace,
            controller=ServiceAccountRef(namespace=namespace, name=account.name),
            accounts_namespace=accounts_namespace,
        )
        # ManagedServiceAccounts' ServiceAccounts, created and pruned only in the accounts namespace.
        accounts_role = Role(
            self,
            "accounts-role",
            metadata=ApiObjectMetadata(name=f"{role_name}-accounts", namespace=accounts_namespace),
            rules=[
                RolePolicyRule(
                    resources=[cast(IApiResource, ApiResource.SERVICE_ACCOUNTS)],
                    verbs=["create", "delete", "get", "list", "watch"],
                )
            ],
        )
        RoleBinding(
            self,
            "accounts-role-binding",
            metadata=ApiObjectMetadata(name=f"{role_name}-accounts", namespace=accounts_namespace),
            role=accounts_role,
        ).add_subjects(account)
        # The workload-identity issuer: it alone writes WorkloadCredential status and workload
        # Certificates, and deletes the Secrets cert-manager issued for superseded generations.
        for workload_namespace in sorted(workload_namespaces):
            issuer_role = Role(
                self,
                f"workload-credentials-role-{workload_namespace}",
                metadata=ApiObjectMetadata(name=f"{role_name}-workload-credentials", namespace=workload_namespace),
                rules=[
                    RolePolicyRule(
                        resources=[custom_resource(_GROUP, "workloadcredentials")], verbs=["get", "list", "watch"]
                    ),
                    RolePolicyRule(
                        resources=[custom_resource(_GROUP, "workloadcredentials/status")], verbs=["update", "patch"]
                    ),
                    # Its Certificates block their record's deletion until they are gone.
                    RolePolicyRule(
                        resources=[custom_resource(_GROUP, "workloadcredentials/finalizers")], verbs=["update"]
                    ),
                    RolePolicyRule(
                        resources=[custom_resource("cert-manager.io", "certificates")],
                        verbs=["create", "get", "list", "watch", "delete"],
                    ),
                    RolePolicyRule(
                        resources=[cast(IApiResource, ApiResource.SECRETS)], verbs=["get", "list", "delete"]
                    ),
                ],
            )
            RoleBinding(
                self,
                f"workload-credentials-role-binding-{workload_namespace}",
                metadata=ApiObjectMetadata(name=f"{role_name}-workload-credentials", namespace=workload_namespace),
                role=issuer_role,
            ).add_subjects(account)
        cluster_role_name = f"{namespace}-policy-controller"
        k8s.KubeClusterRole(
            self,
            "cluster-role",
            metadata=k8s.ObjectMeta(name=cluster_role_name),
            rules=[
                # Its labeled native bindings, and role metadata to retry a binding whose role is missing.
                k8s.PolicyRule(
                    api_groups=[_RBAC],
                    resources=["rolebindings", "clusterrolebindings", "roles", "clusterroles"],
                    verbs=["get", "list", "watch"],
                ),
                k8s.PolicyRule(
                    api_groups=[_RBAC], resources=["clusterrolebindings"], verbs=["create", "delete", "patch"]
                ),
                *(
                    [
                        k8s.PolicyRule(
                            api_groups=[_RBAC], resources=["clusterroles"], resource_names=cluster_roles, verbs=["bind"]
                        )
                    ]
                    if cluster_roles
                    else []
                ),
            ],
        )
        k8s.KubeClusterRoleBinding(
            self,
            "cluster-role-binding",
            metadata=k8s.ObjectMeta(name=cluster_role_name),
            role_ref=k8s.RoleRef(api_group=_RBAC, kind="ClusterRole", name=cluster_role_name),
            subjects=[k8s.Subject(kind="ServiceAccount", name=NAME, namespace=namespace)],
        )
