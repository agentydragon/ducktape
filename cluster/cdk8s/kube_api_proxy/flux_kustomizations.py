"""Flux Kustomizations for the cluster/k8s/kube-api-proxy slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def kube_api_proxy(chart: Chart) -> Kustomization:
    name = "kube-api-proxy"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="kubeapi-proxy", namespace="default"
                ),
                KustomizationSpecHealthChecks(
                    api_version="gateway.networking.k8s.io/v1",
                    kind="HTTPRoute",
                    name="kubeapi-allegedly-works",
                    namespace="default",
                ),
            ],
        ),
    )
