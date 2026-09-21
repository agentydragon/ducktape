"""Flux Kustomizations for the cluster/k8s/headlamp slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def headlamp(chart: Chart, gateway: Kustomization, sso_providers_tf: Kustomization) -> Kustomization:
    name = "headlamp"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="headlamp-app", namespace="ducktape-flux"
            ),
            path="./",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="headlamp"),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="headlamp", namespace="headlamp"
                ),
            ],
            depends_on=flux_kustomization_depends_on_many(gateway, sso_providers_tf),
        ),
    )
