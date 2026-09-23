"""The `nvidia` RuntimeClass GPU workloads select."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts

NAME = "nvidia-runtimeclass"
OUTPUT_DIR = "cluster/k8s/nvidia-runtimeclass"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # The handler is configured by nvidia-container-runtime.cdi in k8s-worker.nix.
    k8s.KubeRuntimeClass(chart, "runtimeclass", metadata=k8s.ObjectMeta(name="nvidia"), handler="nvidia")
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def nvidia_runtimeclass(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
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
