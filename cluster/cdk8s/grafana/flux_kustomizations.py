"""Flux Kustomizations for the cluster/k8s/grafana slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def clickhouse_grafana(chart: Chart, clickhouse: Kustomization, grafana_instance: Kustomization) -> Kustomization:
    name = "clickhouse-grafana"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/grafana",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            wait=True,
            depends_on=[flux_kustomization_depends_on(clickhouse), flux_kustomization_depends_on(grafana_instance)],
        ),
    )
