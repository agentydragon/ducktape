"""Flux Kustomizations for the cluster/k8s/nvidia-runtimeclass slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def nvidia_runtimeclass() -> dict[str, object]:
    name = "nvidia-runtimeclass"
    return flux_kustomization(
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


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/nvidia-runtimeclass/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, nvidia_runtimeclass())
