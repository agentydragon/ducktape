"""A cluster-scoped `ClusterRole`/`ClusterRoleBinding` granting only the ability to
prove a Pod-bound workload bearer via `TokenReview`, and nothing else.

cdk8s_plus_34's fluent `ClusterRole` only supports non-resource-URL rules
(`ClusterRolePolicyRule` takes `endpoints`, not `resources`) -- a resource-scoped
cluster rule like this one needs the tier-2 raw generated binding instead (same tier
as `KubeResourceQuota`/`KubeLimitRange`; see AGENTS.md).
"""

from cdk8s_plus_34 import k8s
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
    k8s.KubeClusterRoleBinding(
        scope,
        f"{id}-binding",
        metadata=k8s.ObjectMeta(name=name),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=name),
        subjects=[k8s.Subject(kind="ServiceAccount", name=service_account_name, namespace=namespace)],
    )
