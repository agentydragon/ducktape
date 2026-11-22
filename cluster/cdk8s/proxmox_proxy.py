"""An nginx reverse proxy on the Proxmox node in front of the Proxmox web UI (TLS to the host,
WebSocket for the noVNC/xterm.js consoles)."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from python.runfiles import Runfiles

from cluster.cdk8s import namespaces
from cluster.cdk8s.container_resources import container_resources
from cluster.cdk8s.flux import ConfigMapArgs
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.scripts import nebula_mesh

NAME = "proxmox-proxy"
NAMESPACE = "proxmox-proxy"
OUTPUT_DIR = f"{GENERATED_ROOT}/proxmox-proxy"
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
_CONFIG_MAP_NAME = "proxmox-proxy-config"
_PROXMOX_HOST = "atlas"
_PROXMOX_UI_PORT = 8006


def config_map(mesh: nebula_mesh.Mesh) -> ConfigMapArgs:
    nginx_conf = (
        (Runfiles.CreateOrRaise().root() / "ducktape/cluster/cdk8s/proxmox-proxy.conf")
        .read_text()
        .replace("@PROXMOX_IP@", str(mesh.hosts[_PROXMOX_HOST].nebula_ip))
        .replace("@PROXMOX_PORT@", str(_PROXMOX_UI_PORT))
        .replace("@LISTEN_PORT@", str(SERVICE.pod_port))
    )
    return ConfigMapArgs(name=_CONFIG_MAP_NAME, namespace=NAMESPACE, data={"nginx.conf": nginx_conf})


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO)
    health = k8s.TcpSocketAction(port=k8s.IntOrString.from_number(SERVICE.pod_port))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=SERVICE.pods.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    automount_service_account_token=False,
                    node_selector={"topology.kubernetes.io/region": "proxmox"},
                    containers=[
                        k8s.Container(
                            name="nginx",
                            image="docker.io/library/nginx:alpine",
                            ports=[SERVICE.k8s_container_port()],
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="config",
                                    mount_path="/etc/nginx/nginx.conf",
                                    sub_path="nginx.conf",
                                    read_only=True,
                                )
                            ],
                            resources=container_resources(
                                cpu="50m", memory="64Mi", cpu_limit="200m", memory_limit="128Mi"
                            ),
                            liveness_probe=k8s.Probe(tcp_socket=health, initial_delay_seconds=5, period_seconds=10),
                            readiness_probe=k8s.Probe(tcp_socket=health, initial_delay_seconds=2, period_seconds=5),
                        )
                    ],
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP_NAME))],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector=SERVICE.pods.selector, ports=[SERVICE.k8s_service_port()]),
    )
    return chart
