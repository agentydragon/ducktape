"""An nginx reverse proxy on the Proxmox node in front of the Proxmox web UI (TLS to the host,
WebSocket for the noVNC/xterm.js consoles)."""

from __future__ import annotations

import textwrap

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on,
)
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
    nginx_conf = textwrap.dedent(f"""\
        worker_processes auto;
        error_log /dev/stderr warn;

        events {{
            worker_connections 128;
        }}

        http {{
            upstream proxmox {{
                server {mesh.hosts[_PROXMOX_HOST].nebula_ip}:{_PROXMOX_UI_PORT};
            }}

            server {{
                listen {SERVICE.pod_port};

                location / {{
                    proxy_pass https://proxmox;
                    proxy_ssl_verify off;

                    proxy_set_header Host $host;
                    proxy_set_header X-Real-IP $remote_addr;
                    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                    proxy_set_header X-Forwarded-Proto $scheme;

                    # WebSocket support (noVNC/xterm.js console)
                    proxy_http_version 1.1;
                    proxy_set_header Upgrade $http_upgrade;
                    proxy_set_header Connection "upgrade";
                }}
            }}
        }}
        """)
    return ConfigMapArgs(name=_CONFIG_MAP_NAME, namespace=NAMESPACE, literals=[f"nginx.conf={nginx_conf}"])


def _tcp_probe(*, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_number(SERVICE.pod_port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO)
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
                    automount_service_account_token=False,
                    node_selector={"topology.kubernetes.io/region": "proxmox"},
                    containers=[
                        k8s.Container(
                            name="nginx",
                            image="docker.io/library/nginx:alpine",
                            ports=[SERVICE.port.k8s_container_port()],
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
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP_NAME))],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector=SERVICE.pods.selector, ports=[SERVICE.port.k8s_service_port()]),
    )
    return chart


def proxmox_proxy(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        suspend=False,
        timeout="5m",
        # Kyverno's failurePolicy: Fail webhooks admit the Deployment.
        depends_on=[flux_kustomization_depends_on(kyverno)],
    )
