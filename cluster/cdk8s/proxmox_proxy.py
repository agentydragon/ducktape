"""An nginx reverse proxy on the Proxmox node in front of the Proxmox web UI (TLS to the host,
WebSocket for the noVNC/xterm.js consoles). Its `nginx.conf` stays a hand-written
`configMapGenerator` input beside the output."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "proxmox-proxy"
NAMESPACE = "proxmox-proxy"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/proxmox-proxy"
_PORT = 8080
_LABELS = {"app.kubernetes.io/name": NAME}
_CONFIG_MAP = ConfigMapArgs(name="proxmox-proxy-config", namespace=NAMESPACE, files=["config/nginx.conf"])


def _tcp_probe(*, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_number(_PORT)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "auto",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
        ),
    )
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(annotations={"reloader.stakater.com/auto": "true"}, labels=_LABELS),
                spec=k8s.PodSpec(
                    node_selector={"topology.kubernetes.io/region": "proxmox"},
                    containers=[
                        k8s.Container(
                            name="nginx",
                            image="docker.io/library/nginx:alpine",
                            ports=[k8s.ContainerPort(container_port=_PORT, name="http")],
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="config",
                                    mount_path="/etc/nginx/nginx.conf",
                                    sub_path="nginx.conf",
                                    read_only=True,
                                )
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("200m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                            ),
                            liveness_probe=_tcp_probe(initial_delay_seconds=5, period_seconds=10),
                            readiness_probe=_tcp_probe(initial_delay_seconds=2, period_seconds=5),
                        )
                    ],
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP.name))],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(port=_PORT, target_port=k8s.IntOrString.from_number(_PORT), protocol="TCP", name="http")
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"], config_map_generator=[_CONFIG_MAP]),
    )


def proxmox_proxy(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, gateway: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, NAME, artifact, suspend=False, timeout="5m", depends_on=[flux_kustomization_depends_on(gateway)]
    )
