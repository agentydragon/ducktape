"""Flux Kustomizations for the cluster/k8s/nvidia-runtimeclass slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def nvidia_runtimeclass(chart: Chart) -> Kustomization:
    name = "nvidia-runtimeclass"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="2m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/nvidia-runtimeclass",
            prune=True,
        ),
        description="NVIDIA RuntimeClass prerequisite for GPU workloads.",
    )
