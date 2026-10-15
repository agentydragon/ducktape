"""The RBAC granted to the Agentplane Policy Controller for assignable Kubernetes grants."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ApiResource, IApiResource, Role, RoleBinding, RolePolicyRule, ServiceAccount, k8s
from constructs import Construct

from agentplane.sandboxes.kubernetes_grants import KubernetesGrant, RoleBindingGrant
from cluster.cdk8s.agentplane.environment import Environment
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
    ) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
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
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            rules=[
                RolePolicyRule(resources=[custom_resource(_GROUP, "policysets")], verbs=["get", "list", "watch"]),
                # Deleting is how a binding expires.
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "policybindings")], verbs=["get", "list", "watch", "delete"]
                ),
                RolePolicyRule(resources=[custom_resource(_GROUP, "policybindings/status")], verbs=["patch"]),
                RolePolicyRule(
                    resources=[cast(IApiResource, ApiResource.SERVICE_ACCOUNTS)], verbs=["get", "list", "watch"]
                ),
                # Leader election.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")], verbs=["get", "create", "update"]
                ),
                RolePolicyRule(resources=[custom_resource("", "events")], verbs=["create", "patch"]),
                # RoleBindings in its own namespace; binding_delegation.py grants the same elsewhere.
                RolePolicyRule(resources=[custom_resource(_RBAC, "rolebindings")], verbs=["create", "delete"]),
                *(
                    RolePolicyRule(resources=[named_resource(_RBAC, "roles", name)], verbs=["bind"])
                    for name in own_roles
                ),
            ],
        )
        RoleBinding(
            self,
            "role-binding",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
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
                k8s.PolicyRule(api_groups=[_RBAC], resources=["clusterrolebindings"], verbs=["create", "delete"]),
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
