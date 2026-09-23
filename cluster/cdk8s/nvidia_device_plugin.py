"""The NVIDIA device plugin, which advertises `nvidia.com/gpu` on GPU nodes."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "nvidia-device-plugin"
NAMESPACE = "nvidia-device-plugin"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/nvidia-device-plugin"


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
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata("nvidia", NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://nvidia.github.io/k8s-device-plugin"),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
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


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def nvidia_device_plugin(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    nvidia_runtimeclass: Kustomization,
    node_feature_discovery: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(nvidia_runtimeclass, node_feature_discovery),
    )
