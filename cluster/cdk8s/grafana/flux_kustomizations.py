"""Flux Kustomizations for the cluster/k8s/grafana slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def clickhouse_grafana(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, clickhouse: Kustomization, grafana_instance: Kustomization
) -> Kustomization:
    name = "clickhouse-grafana"
    return flux_kustomization(
        chart, name, artifact, timeout="5m", depends_on=flux_kustomization_depends_on_many(clickhouse, grafana_instance)
    )
