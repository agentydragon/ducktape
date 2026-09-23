"""grafana-operator, which runs the Grafana instance and reconciles its dashboards,
datasources and service accounts."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import HelmReleaseSpecDriftDetection, HelmReleaseSpecDriftDetectionMode
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec, HelmRepositorySpecType
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.metadata import metadata

NAME = "grafana-operator"
OUTPUT_DIR = "cluster/k8s/monitoring/grafana-operator"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    repository = HelmRepository(
        chart,
        "helm-repository",
        metadata=metadata(NAME, "flux-system"),
        spec=HelmRepositorySpec(
            type=HelmRepositorySpecType.OCI, url="oci://ghcr.io/grafana/helm-charts", interval="12h"
        ),
    )
    helm_release(
        chart,
        NAME,
        "monitoring",
        repository=repository,
        chart=NAME,
        version="~5.22",
        interval="30m",
        chart_interval="12h",
        # 2026-05-24: nothing else writes the operator's Deployment, so it's
        # safe to have Flux re-apply on drift (e.g. accidental kubectl scale).
        drift_detection=HelmReleaseSpecDriftDetection(mode=HelmReleaseSpecDriftDetectionMode.ENABLED),
        values={
            "resources": {"limits": {"cpu": "200m", "memory": "128Mi"}, "requests": {"cpu": "50m", "memory": "64Mi"}}
        },
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def grafana_operator(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_namespace: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "grafana-operator",
        artifact,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2",
                kind="HelmRelease",
                name="grafana-operator",
                namespace="monitoring",
            )
        ],
        timeout="5m",
        depends_on=[flux_kustomization_depends_on(monitoring_namespace)],
    )
