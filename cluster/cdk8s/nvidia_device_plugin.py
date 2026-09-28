"""The NVIDIA device plugin, which advertises `nvidia.com/gpu` on GPU nodes."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "nvidia-device-plugin"
NAMESPACE = "nvidia-device-plugin"
OUTPUT_DIR = f"{GENERATED_ROOT}/nvidia-device-plugin"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={"pod-security.kubernetes.io/enforce": "privileged", "rbac.ducktape.io/agent-readable-logs": "true"},
        ),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, "nvidia", NAMESPACE, url="https://nvidia.github.io/k8s-device-plugin"),
        chart=NAME,
        version="0.20.0",
        interval="15m",
        install=RETRY_FAILED_INSTALL,
        values={
            # nvidia-container-runtime.cdi (in containerd, via RuntimeClass) injects
            # /dev/nvidia*, driver libs, and glibc via host CDI specs. The device plugin
            # only needs NVML access (provided by the runtime) to enumerate GPUs.
            # Default envvar strategy: plugin sets NVIDIA_VISIBLE_DEVICES on workload
            # containers; the CDI runtime translates that to CDI device injection.
            "runtimeClassName": "nvidia"
        },
    )
    return chart


def nvidia_device_plugin(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, timeout="5m")
