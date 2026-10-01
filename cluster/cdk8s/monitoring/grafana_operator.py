"""grafana-operator, which runs the Grafana instance and reconciles its dashboards,
datasources and service accounts."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import HelmReleaseSpecDriftDetection, HelmReleaseSpecDriftDetectionMode
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import helm_release, oci_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "grafana-operator"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/grafana-operator"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    helm_release(
        chart,
        NAME,
        "monitoring",
        repository=oci_helm_repository(chart, NAME, "flux-system", url="oci://ghcr.io/grafana/helm-charts"),
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


def grafana_operator(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(
        chart,
        "grafana-operator",
        directory,
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
    )
