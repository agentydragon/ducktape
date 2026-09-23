"""Flux Kustomizations for the cluster/k8s/nvidia-runtimeclass slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization


def nvidia_runtimeclass(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "nvidia-runtimeclass"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="2m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
        ),
        description="NVIDIA RuntimeClass prerequisite for GPU workloads.",
    )
