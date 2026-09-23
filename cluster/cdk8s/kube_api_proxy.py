"""`kubeapi.allegedly.works`: the Gateway route and the nginx reverse proxy that bridges HTTP
to HTTPS in front of the Kubernetes API (`cluster/k8s/kube-api-proxy/README.md`).

Cilium Gateway API doesn't support backend TLS re-encryption (no BackendTLSPolicy, no
`appProtocol: https`), so nginx accepts plain HTTP from the Gateway after TLS termination and
proxies to the apiserver over HTTPS, preserving the Authorization header.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "kube-api-proxy"
NAMESPACE = "default"
OUTPUT_DIR = "cluster/k8s/kube-api-proxy"
_PROXY = "kubeapi-proxy"
_CONFIG_MAP = "kubeapi-proxy-config"
_ROUTE = "kubeapi-allegedly-works"
_PORT = 8080
_LABELS = {"app": _PROXY}
_NGINX_CONF = """\
pid /tmp/nginx.pid;
worker_processes 1;
error_log /dev/stderr warn;
events { worker_connections 128; }
http {
  access_log /dev/stderr;
  # kubectl exec/attach/port-forward open an HTTP Upgrade (WebSocket/SPDY) to
  # the apiserver. By default nginx proxies as HTTP/1.0 and strips the hop-by-hop
  # Upgrade/Connection headers, so the apiserver receives a plain GET to /exec and
  # returns 400 "Upgrade request required". The map + directives below fix that.
  map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
  }
  server {
    listen 8080;
    location / {
      proxy_pass https://kubernetes.default.svc:443;
      proxy_ssl_verify on;
      proxy_ssl_trusted_certificate /var/run/secrets/kubernetes.io/serviceaccount/ca.crt;
      proxy_ssl_server_name on;
      proxy_ssl_name kubernetes.default.svc;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      # Pass through all headers including Authorization
      proxy_pass_request_headers on;
      # WebSocket/SPDY upgrade for kubectl exec/attach/port-forward:
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_read_timeout 300s;
      proxy_send_timeout 300s;
    }
  }
}
"""


def _deployment(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_PROXY,
            namespace=NAMESPACE,
            annotations={
                # Restart pods when the config changes (subPath mounts don't hot-reload).
                "reloader.stakater.com/auto": "true",
                "description": (
                    "nginx reverse proxy: HTTP 8080 → HTTPS kubernetes.default.svc:443.\n"
                    "Bridges the gap between Cilium Gateway (TLS terminate) and the\n"
                    "apiserver (requires HTTPS). Used by kubeapi.allegedly.works route.\n"
                ),
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=2,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=True,
                    containers=[
                        k8s.Container(
                            name="nginx",
                            image="nginxinc/nginx-unprivileged:1.31-alpine",
                            ports=[k8s.ContainerPort(container_port=_PORT)],
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="config",
                                    mount_path="/etc/nginx/nginx.conf",
                                    sub_path="nginx.conf",
                                    read_only=True,
                                ),
                                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                                k8s.VolumeMount(name="cache", mount_path="/var/cache/nginx"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("16Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("64Mi")},
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                read_only_root_filesystem=True,
                                run_as_non_root=True,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP)),
                        k8s.Volume(
                            name="tmp",
                            empty_dir=k8s.EmptyDirVolumeSource(
                                medium="Memory", size_limit=k8s.Quantity.from_string("1Mi")
                            ),
                        ),
                        k8s.Volume(
                            name="cache",
                            empty_dir=k8s.EmptyDirVolumeSource(
                                medium="Memory", size_limit=k8s.Quantity.from_string("8Mi")
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    https_route(
        chart,
        "route",
        metadata=metadata(
            _ROUTE,
            NAMESPACE,
            annotations={
                "description": (
                    "HTTPRoute for the Kubernetes API at kubeapi.allegedly.works. The Cilium\n"
                    "Gateway terminates the wildcard LE cert at the `https-wildcard` listener,\n"
                    "then forwards to the `kubeapi-proxy` nginx pod which re-encrypts to the\n"
                    "apiserver over HTTPS (Cilium doesn't support backend TLS natively).\n"
                    "\n"
                    "Purpose: works through Claude Code web's L7 TLS-terminating egress proxy,\n"
                    "which rejects the cluster-CA-signed cert that api.allegedly.works presents\n"
                    "on the direct API endpoint (:6443). See ../docs/lessons_learned/ for the full story.\n"
                    "Auth is bearer-JWT only (client certs die at the MITM boundary; see the\n"
                    "kubeconfig.py docstring in devinfra/k8s/kubeconfig.py).\n"
                )
            },
        ),
        hostname="kubeapi.allegedly.works",
        backend=_PROXY,
        port=_PORT,
        hsts=False,
    )
    k8s.KubeConfigMap(
        chart,
        "config",
        metadata=k8s.ObjectMeta(name=_CONFIG_MAP, namespace=NAMESPACE),
        data={"nginx.conf": _NGINX_CONF},
    )
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_PROXY, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(name="http", port=_PORT, target_port=k8s.IntOrString.from_number(_PORT), protocol="TCP")
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))


def kube_api_proxy(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name=_PROXY, namespace=NAMESPACE
                ),
                KustomizationSpecHealthChecks(
                    api_version="gateway.networking.k8s.io/v1", kind="HTTPRoute", name=_ROUTE, namespace=NAMESPACE
                ),
            ],
        ),
    )
