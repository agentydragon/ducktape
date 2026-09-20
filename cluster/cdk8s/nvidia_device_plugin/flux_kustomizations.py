"""Flux Kustomizations for the cluster/k8s/nvidia-device-plugin slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def nvidia_device_plugin(
    chart: Chart, nvidia_runtimeclass: Kustomization, node_feature_discovery: Kustomization
) -> Kustomization:
    name = "nvidia-device-plugin"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/nvidia-device-plugin",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="nvidia",
                    namespace="nvidia-device-plugin",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(nvidia_runtimeclass, node_feature_discovery),
        ),
    )
