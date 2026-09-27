"""The `nvidia` RuntimeClass GPU workloads select."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "nvidia-runtimeclass"
OUTPUT_DIR = f"{GENERATED_ROOT}/nvidia-runtimeclass"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # The handler is configured by nvidia-container-runtime.cdi in k8s-worker.nix.
    k8s.KubeRuntimeClass(chart, "runtimeclass", metadata=k8s.ObjectMeta(name="nvidia"), handler="nvidia")
    return chart


def nvidia_runtimeclass(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        wait=None,
        timeout="2m",
        description="NVIDIA RuntimeClass prerequisite for GPU workloads.",
    )
