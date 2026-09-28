"""plaid-mcp's `reader/`: the read-only Postgres MCP over the Plaid sync database, fronted by
mcp-oauth-facade, with its config, Service, HTTPRoute and ingress policy; and
`servicemonitor/`, which scrapes the facade.

The facade's image tag is the placeholder "unset"; the hand-written
`reader/image-pins/kustomization.yaml` overrides it at `kustomize build` time via Flux's
image-automation marker (cluster/cdk8s/AGENTS.md § the `:tag` Setters marker). Also
hand-written in `reader/`: `kustomization.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu, k8s
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import cilium
from cluster.cdk8s.forgejo_images import SECRET_NAME
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.plaid_mcp import db
from cluster.cdk8s.plaid_mcp.app import NAMESPACE
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.valkey import valkey_instance

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp/reader"
SERVICEMONITOR_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp/servicemonitor"
_NAME = "plaid-db-mcp"
_LABELS = {"app.kubernetes.io/name": _NAME}
_CONFIG_MAP = "plaid-db-mcp-config"
_OIDC = SecretRef(namespace=NAMESPACE, name="plaid-db-mcp-oidc")
_UPSTREAM_PORT = 8000
_HTTP_PORT = 8765
_METRICS_PORT = 9090
_HTTP = ServiceRef(
    name=_NAME, port=Port(name="http", number=_HTTP_PORT), pods=Pods(namespace=NAMESPACE, labels=tuple(_LABELS.items()))
)
_VALKEY = "plaid-valkey-kimsufi"


def _container_security_context() -> k8s.SecurityContext:
    return k8s.SecurityContext(
        allow_privilege_escalation=False,
        capabilities=k8s.Capabilities(drop=["ALL"]),
        run_as_non_root=True,
        run_as_group=1000,
        run_as_user=1000,
    )


def _deployment(chart: Chart) -> None:
    upstream = k8s.TcpSocketAction(port=k8s.IntOrString.from_number(_UPSTREAM_PORT))
    health = k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_HTTP_PORT))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=NAMESPACE,
            labels=_LABELS,
            annotations={
                "description": (
                    "Read-only Postgres MCP over the Plaid sync database, fronted by mcp-oauth-facade. The"
                    " upstream MCP uses Streamable HTTP and connects with the plaid_ro role."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    containers=[
                        k8s.Container(
                            name="postgres-mcp",
                            # renovate: datasource=docker
                            image="enterprisedb/pg-airman-mcp:latest@sha256:99fb30356e66b7ebd816dbbbf70a29f091bcc1db09cf952bc719f10a05818c04",
                            image_pull_policy="IfNotPresent",
                            args=[
                                "--access-mode=restricted",
                                "--transport=streamable-http",
                                "--streamable-http-host=0.0.0.0",
                                f"--streamable-http-port={_UPSTREAM_PORT}",
                            ],
                            ports=[k8s.ContainerPort(name="upstream", container_port=_UPSTREAM_PORT, protocol="TCP")],
                            env=[db.READONLY.key("DATABASE_URL").env_var("AIRMAN_MCP_DATABASE_URL")],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                limits={"memory": k8s.Quantity.from_string("512Mi")},
                            ),
                            security_context=_container_security_context(),
                            readiness_probe=k8s.Probe(tcp_socket=upstream, initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=k8s.Probe(tcp_socket=upstream, initial_delay_seconds=20, period_seconds=20),
                        ),
                        k8s.Container(
                            name="facade",
                            image="git.allegedly.works/ducktape-ci/mcp-oauth-facade:unset",
                            image_pull_policy="Always",
                            ports=[
                                k8s.ContainerPort(name="http", container_port=_HTTP_PORT, protocol="TCP"),
                                # Prometheus metrics, cluster-internal only (not on the HTTPRoute).
                                k8s.ContainerPort(name="metrics", container_port=_METRICS_PORT, protocol="TCP"),
                            ],
                            env_from=[k8s.EnvFromSource(config_map_ref=k8s.ConfigMapEnvSource(name=_CONFIG_MAP))],
                            env=[
                                _OIDC.key("client_id").env_var("MCP_FACADE_AUTH__OIDC_CLIENT_ID"),
                                _OIDC.key("client_secret").env_var("MCP_FACADE_AUTH__OIDC_CLIENT_SECRET"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                limits={"memory": k8s.Quantity.from_string("256Mi")},
                            ),
                            security_context=_container_security_context(),
                            readiness_probe=k8s.Probe(http_get=health, initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=k8s.Probe(http_get=health, initial_delay_seconds=20, period_seconds=20),
                        ),
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    k8s.KubeConfigMap(
        chart,
        "config",
        metadata=k8s.ObjectMeta(name=_CONFIG_MAP, namespace=NAMESPACE),
        data={
            "MCP_FACADE_AUTH__OIDC_ISSUER": "https://auth.allegedly.works/application/o/plaid-db-mcp/",
            "MCP_FACADE_AUTH__PUBLIC_BASE_URL": "https://plaid-db.allegedly.works",
            "MCP_FACADE_FACADE_NAME": "Plaid DB MCP Facade",
            "MCP_FACADE_UPSTREAM__KIND": "http",
            "MCP_FACADE_UPSTREAM__URL": f"http://localhost:{_UPSTREAM_PORT}/mcp",
            "MCP_FACADE_PERSISTENCE__KIND": "valkey",
            # The RedisReplication's primary Service.
            "MCP_FACADE_PERSISTENCE__HOST": f"{_VALKEY}-master.{NAMESPACE}.svc.cluster.local",
            "MCP_FACADE_PERSISTENCE__DB": "0",
        },
    )
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(
                    name="http", port=_HTTP_PORT, target_port=k8s.IntOrString.from_string("http"), protocol="TCP"
                ),
                k8s.ServicePort(
                    name="metrics",
                    port=_METRICS_PORT,
                    target_port=k8s.IntOrString.from_string("metrics"),
                    protocol="TCP",
                ),
            ],
            type="ClusterIP",
        ),
    )
    https_route(
        chart,
        "httproute",
        metadata=ApiObjectMetadata(
            name=_NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Authentik-gated Postgres MCP for querying the synced Plaid database through the read-only"
                    " plaid_ro role."
                )
            },
        ),
        hostnames=["plaid-db.allegedly.works"],
        backend=_HTTP,
        timeout="60s",
        hsts=False,
        listener=None,
    )
    NetworkPolicy(
        chart,
        "ingress-policy",
        metadata=ApiObjectMetadata(
            name="plaid-db-mcp-ingress",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Default-deny ingress for plaid-db-mcp pods. Only Gateway ingress may reach the OAuth facade;"
                    " postgres-mcp stays loopback-only."
                )
            },
        ),
        endpoint_selector=_LABELS,
        ingress=[
            IngressRule.from_gateway(_HTTP_PORT),
            # monitoring: Prometheus metrics scraping
            cilium.SCRAPERS.admit(_METRICS_PORT),
        ],
    )
    valkey_instance(
        chart,
        name=_VALKEY,
        namespace=NAMESPACE,
        description="Kimsufi Valkey for the Plaid DB MCP OAuth facade state",
        memory_request=Size.mebibytes(64),
        cpu_limit=Cpu.millis(200),
        memory_limit=Size.mebibytes(128),
        max_memory_percent_of_limit=None,
        storage_class="local-path-ovh",
        storage_size=Size.gibibytes(1),
    )
    return chart


def servicemonitor_chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    ServiceMonitor(
        chart,
        "servicemonitor",
        metadata=ApiObjectMetadata(name=_NAME, namespace=NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
        endpoints=[Endpoint.plain(port="metrics", scrape_timeout="10s")],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_charts(root, SERVICEMONITOR_DIR, servicemonitor_chart)
