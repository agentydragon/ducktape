"""The `grafana` HelmRepository that loki, mimir, tempo and alloy install from."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import helm_repository_source_ref
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/grafana-helmrepository"
_NAME = "grafana"
_NAMESPACE = "flux-system"
# The releases installing from this repository live in other charts.
SOURCE_REF = helm_repository_source_ref(_NAME, _NAMESPACE)


def chart(app: App) -> Chart:
    chart = Chart(app, "helmrepository", disable_resource_name_hashes=True)
    HelmRepository(
        chart,
        "grafana",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        spec=HelmRepositorySpec(interval="12h", url="https://grafana.github.io/helm-charts"),
    )
    return chart


def grafana_helmrepository(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, "grafana-helmrepository", directory, timeout="5m")
