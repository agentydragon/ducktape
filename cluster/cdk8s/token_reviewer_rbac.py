"""A cluster-scoped `ClusterRole`/`ClusterRoleBinding` granting only the ability to
prove a Pod-bound workload bearer via `TokenReview`, and nothing else.

cdk8s_plus_34's fluent `ClusterRole` only supports non-resource-URL rules
(`ClusterRolePolicyRule` takes `endpoints`, not `resources`), so the resource-scoped
role uses the generated `KubeClusterRole`. Its binding uses the fluent
`ClusterRoleBinding` construct.
"""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ClusterRole, ClusterRoleBinding, ServiceAccount, k8s
from constructs import Construct


def token_reviewer_cluster_rbac(
    scope: Construct, id: str, *, name: str, service_account_name: str, namespace: str
) -> None:
    k8s.KubeClusterRole(
        scope,
        f"{id}-role",
        metadata=k8s.ObjectMeta(name=name),
        rules=[k8s.PolicyRule(api_groups=["authentication.k8s.io"], resources=["tokenreviews"], verbs=["create"])],
    )
    ClusterRoleBinding(
        scope,
        f"{id}-binding",
        metadata=ApiObjectMetadata(name=name),
        role=ClusterRole.from_cluster_role_name(scope, f"{id}-role-ref", name),
    ).add_subjects(
        ServiceAccount.from_service_account_name(
            scope, f"{id}-service-account-ref", service_account_name, namespace_name=namespace
        )
    )
