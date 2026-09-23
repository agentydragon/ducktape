"""The `grafana` HelmRepository that loki, mimir, tempo and alloy install from."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import helm_repository_source_ref
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/monitoring/grafana-helmrepository"
_NAME = "grafana"
_NAMESPACE = "flux-system"
# The releases installing from this repository live in other charts.
SOURCE_REF = helm_repository_source_ref(_NAME, _NAMESPACE)


def chart(app: App) -> Chart:
    chart = Chart(app, "helmrepository", disable_resource_name_hashes=True)
    HelmRepository(
        chart,
        "grafana",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HelmRepositorySpec(interval="12h", url="https://grafana.github.io/helm-charts"),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def grafana_helmrepository(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(chart, "grafana-helmrepository", artifact, timeout="5m")
