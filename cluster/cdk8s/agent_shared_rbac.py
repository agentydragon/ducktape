"""Cluster-scoped agent RBAC (cluster/generated/agents/shared-rbac): the ClusterRoleBinding that
grants every agent identity the secret-free cluster-diagnostics-reader ClusterRole.
Namespace-scoped RoleBindings live in per-service agent-rbac/ directories
(cluster/docs/agent_rbac.md).
"""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import agent_access_profiles as access
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
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


def agent_shared_rbac(chart: Chart, directory: RenderedDirectory, claude_rbac: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        retry_interval=None,
        wait=None,
        timeout="2m",
        depends_on=flux_kustomization_depends_on_many(claude_rbac),
        description=(
            "Cluster-scoped agent RBAC (ClusterRoleBindings) + flux-system "
            "RoleBindings only. Namespace-scoped RoleBindings live in per-service "
            "agent-rbac/ directories."
        ),
    )
