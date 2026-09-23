"""The `nvidia` RuntimeClass GPU workloads select."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "nvidia-runtimeclass"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/nvidia-runtimeclass"


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
        artifact,
        wait=None,
        timeout="2m",
        description="NVIDIA RuntimeClass prerequisite for GPU workloads.",
    )
