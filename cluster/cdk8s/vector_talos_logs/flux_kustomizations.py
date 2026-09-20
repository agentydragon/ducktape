"""Flux Kustomizations for the cluster/k8s/vector-talos-logs slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def vector_talos_logs(chart: Chart, loki: Kustomization) -> Kustomization:
    name = "vector-talos-logs"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/vector-talos-logs",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=[
                # loki-write is the log sink.
                flux_kustomization_depends_on(loki)
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="DaemonSet", name="vector-talos-logs", namespace="vector-talos-logs"
                )
            ],
        ),
    )
