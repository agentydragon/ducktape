"""The `monitoring` Namespace."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml

NAMESPACE = "monitoring"
OUTPUT_DIR = "cluster/k8s/monitoring/namespace"


def chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "initial",
                "rbac.ducktape.io/agent-readable-logs": "true",
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/audit": "privileged",
                "pod-security.kubernetes.io/warn": "privileged",
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=["namespace.k8s.yaml"]))


def monitoring_namespace(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-namespace",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            wait=True,
            # Health check ensures monitoring namespace exists before dependents deploy
            health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name=NAMESPACE)],
            depends_on=[],
        ),
    )
