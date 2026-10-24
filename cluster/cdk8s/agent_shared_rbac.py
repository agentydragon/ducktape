"""Cluster-scoped agent RBAC (cluster/generated/agents/shared-rbac).

The shared-rbac chart binds the secret-free cluster-diagnostics-reader to its existing
cluster-diagnostics identities, plus separately selected resource readers to their profile
identities. Namespace-scoped RoleBindings live in per-service agent-rbac/ directories
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
    k8s.KubeClusterRoleBinding(
        chart,
        "redisreplication-reader",
        metadata=k8s.ObjectMeta(name="agent-redisreplication-reader"),
        role_ref=access.role_ref("redisreplication-read"),
        subjects=[subject.k8s() for subject in access.profile_subjects("redisreplication-read")],
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
