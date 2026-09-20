"""Flux Kustomizations for the cluster/k8s/flux-monitoring slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def flux_monitoring(chart: Chart, monitoring_crds: Kustomization) -> Kustomization:
    name = "flux-monitoring"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="2m",
            path="./cluster/k8s/flux-monitoring",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                # PodMonitor CRD ships with kube-prometheus-stack in monitoring-stack.
                # PodMonitor
                flux_kustomization_depends_on(monitoring_crds)
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="monitoring.coreos.com/v1", kind="PodMonitor", name="ducktape", namespace="flux-system"
                )
            ],
        ),
    )
