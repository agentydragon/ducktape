"""grafana-operator, which runs the Grafana instance and reconciles its dashboards,
datasources and service accounts."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecDriftDetection,
    HelmReleaseSpecDriftDetectionMode,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec, HelmRepositorySpecType
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "grafana-operator"
OUTPUT_DIR = "cluster/k8s/monitoring/grafana-operator"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    HelmRepository(
        chart,
        "helm-repository",
        metadata=metadata(NAME, "flux-system"),
        spec=HelmRepositorySpec(
            type=HelmRepositorySpecType.OCI, url="oci://ghcr.io/grafana/helm-charts", interval="12h"
        ),
    )
    HelmRelease(
        chart,
        "helm-release",
        metadata=metadata(NAME, "monitoring"),
        spec=HelmReleaseSpec(
            interval="30m",
            # 2026-05-24: nothing else writes the operator's Deployment, so it's
            # safe to have Flux re-apply on drift (e.g. accidental kubectl scale).
            drift_detection=HelmReleaseSpecDriftDetection(mode=HelmReleaseSpecDriftDetectionMode.ENABLED),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=NAME,
                    # renovate: datasource=docker depName=ghcr.io/grafana/helm-charts/grafana-operator versioning=helm
                    version="~5.22",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY, name=NAME, namespace="flux-system"
                    ),
                    interval="12h",
                )
            ),
            values={
                "resources": {
                    "limits": {"cpu": "200m", "memory": "128Mi"},
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                }
            },
        ),
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
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
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
        ),
    )
