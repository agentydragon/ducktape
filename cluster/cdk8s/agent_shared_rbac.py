"""Cluster-scoped agent RBAC (cluster/generated/agents/shared-rbac): the ClusterRoleBinding that
grants existing agent identities the secret-free cluster-diagnostics-reader ClusterRole.
Namespace-scoped RoleBindings live in per-service agent-rbac/ directories
(cluster/docs/agent_rbac.md).
"""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import agent_access_profiles as access
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "agent-shared-rbac"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/shared-rbac"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeClusterRoleBinding(
        chart,
        "cluster-diagnostics-reader",
        metadata=k8s.ObjectMeta(name="agent-cluster-diagnostics-reader"),
        role_ref=access.role_ref("cluster-diagnostics"),
        subjects=[subject.k8s() for subject in access.CLUSTER_DIAGNOSTIC_SUBJECTS],
    )
    return chart
