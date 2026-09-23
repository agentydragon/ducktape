"""Cluster-scoped agent RBAC (cluster/k8s/agents/shared-rbac): the ClusterRoleBinding that
grants every agent identity the secret-free cluster-diagnostics-reader ClusterRole.
Namespace-scoped RoleBindings live in per-service agent-rbac/ directories
(cluster/k8s/agents/agent-rbac-base/README.md).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml

NAME = "agent-shared-rbac"
OUTPUT_DIR = "cluster/k8s/agents/shared-rbac"
_RBAC_GROUP = "rbac.authorization.k8s.io"


def _group(name: str) -> k8s.Subject:
    return k8s.Subject(kind="Group", name=name, api_group=_RBAC_GROUP)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeClusterRoleBinding(
        chart,
        "cluster-diagnostics-reader",
        metadata=k8s.ObjectMeta(name="agent-cluster-diagnostics-reader"),
        role_ref=k8s.RoleRef(api_group=_RBAC_GROUP, kind="ClusterRole", name="cluster-diagnostics-reader"),
        subjects=[
            _group("oidc-ksbx-groups:kubectl-sandbox-users"),
            # Haku background agent: secret-free cluster-wide diagnostics. This class grants
            # no secrets/pods-log/configmaps, so Haku reads no new credential material through
            # it. Log/configmap reads are granted separately per infra namespace, not here.
            _group("oidc-ksbx-groups:haku"),
            _group("haku:access-profile:haku"),
            k8s.Subject(kind="ServiceAccount", name="haku", namespace="haku-sandbox"),
            # agent-box Codex VM: secret-free cluster diagnostics only. Namespace/log
            # expansion stays explicit in per-namespace RoleBindings.
            _group("oidc-ksbx-groups:agent-box-codex"),
            # claude-ai: the principal for Connections enrolled from the Claude.ai MCP connector
            # (cluster/cdk8s/agentplane/actions_staging_policies.py), and every sandbox stamped
            # for it (agentplane/docs/sandbox_actions.md). Secret-free cluster diagnostics here;
            # the agent-readable namespace readers (cluster/cdk8s/kyverno/policies.py's
            # generate-agent-diagnostics-readers) add metadata and pod logs where a namespace opts in.
            k8s.Subject(kind="ServiceAccount", name="claude-ai", namespace="agentplane-staging"),
        ],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))


def agent_shared_rbac(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, claude_rbac: Kustomization, kyverno_policies: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="2m",
            depends_on=flux_kustomization_depends_on_many(claude_rbac, kyverno_policies),
        ),
        description=(
            "Cluster-scoped agent RBAC (ClusterRoleBindings) + flux-system "
            "RoleBindings only. Namespace-scoped RoleBindings live in per-service "
            "agent-rbac/ directories."
        ),
    )
