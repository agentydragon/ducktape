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

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import cilium
from cluster.cdk8s.forgejo_images import SECRET_NAME
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.mcp_oauth_state import CONSUMER_SECRET, PLAID_DB, add_consumer_credentials
from cluster.cdk8s.plaid_mcp import db
from cluster.cdk8s.plaid_mcp.db import NAMESPACE
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp/reader"
SERVICEMONITOR_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp/servicemonitor"
_NAME = "plaid-db-mcp"
_CONFIG_MAP = "plaid-db-mcp-config"
_OIDC = SecretRef(namespace=NAMESPACE, name="plaid-db-mcp-oidc")
# postgres-mcp, which the facade reaches over the Pod's loopback.
_UPSTREAM = Port(name="upstream", number=8000)
_HTTP = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=8765),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),)),
)
_METRICS = ServiceRef(name=_HTTP.name, port=Port(name="metrics", number=9090), pods=_HTTP.pods)


def _container_security_context() -> k8s.SecurityContext:
    return k8s.SecurityContext(
        allow_privilege_escalation=False,
        capabilities=k8s.Capabilities(drop=["ALL"]),
        run_as_non_root=True,
        run_as_group=1000,
        run_as_user=1000,
    )


def _deployment(chart: Chart) -> None:
    upstream = k8s.TcpSocketAction(port=k8s.IntOrString.from_number(_UPSTREAM.number))
    health = k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_HTTP.pod_port))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=NAMESPACE,
            labels=_HTTP.pods.selector,
            annotations={
                "description": (
                    "Read-only Postgres MCP over the Plaid sync database, fronted by mcp-oauth-facade. The"
                    " upstream MCP uses Streamable HTTP and connects with the plaid_ro role."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_HTTP.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_HTTP.pods.selector),
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
                                f"--streamable-http-port={_UPSTREAM.number}",
                            ],
                            ports=[_UPSTREAM.k8s_container_port()],
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
                                _HTTP.port.k8s_container_port(),
                                # Prometheus metrics, cluster-internal only (not on the HTTPRoute).
                                _METRICS.port.k8s_container_port(),
                            ],
                            env_from=[k8s.EnvFromSource(config_map_ref=k8s.ConfigMapEnvSource(name=_CONFIG_MAP))],
                            env=[
                                _OIDC.key("client_id").env_var("MCP_FACADE_AUTH__OIDC_CLIENT_ID"),
                                _OIDC.key("client_secret").env_var("MCP_FACADE_AUTH__OIDC_CLIENT_SECRET"),
                                SecretRef(namespace=NAMESPACE, name=CONSUMER_SECRET)
                                .key("uri")
                                .env_var("MCP_FACADE_PERSISTENCE__URL"),
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
    add_consumer_credentials(chart, PLAID_DB)
    k8s.KubeConfigMap(
        chart,
        "config",
        metadata=k8s.ObjectMeta(name=_CONFIG_MAP, namespace=NAMESPACE),
        data={
            "MCP_FACADE_AUTH__OIDC_ISSUER": "https://auth.allegedly.works/application/o/plaid-db-mcp/",
            "MCP_FACADE_AUTH__PUBLIC_BASE_URL": "https://plaid-db.allegedly.works",
            "MCP_FACADE_FACADE_NAME": "Plaid DB MCP Facade",
            "MCP_FACADE_UPSTREAM__KIND": "http",
            "MCP_FACADE_UPSTREAM__URL": f"http://localhost:{_UPSTREAM.number}/mcp",
            "MCP_FACADE_PERSISTENCE__KIND": "postgres",
        },
    )
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_HTTP.name, namespace=NAMESPACE, labels=_HTTP.labels),
        spec=k8s.ServiceSpec(
            selector=_HTTP.pods.selector,
            ports=[_HTTP.port.k8s_service_port(), _METRICS.port.k8s_service_port()],
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
        endpoint_selector=_HTTP.pods.selector,
        ingress=[IngressRule.from_gateway(_HTTP.pod_port), cilium.SCRAPERS.admit(_METRICS.pod_port)],
    )
    return chart


def servicemonitor_chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    ServiceMonitor(
        chart,
        "servicemonitor",
        metadata=ApiObjectMetadata(name=_NAME, namespace=NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=_HTTP.labels),
        endpoints=[Endpoint.plain(port=_METRICS.port.name, scrape_timeout="10s")],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_charts(root, SERVICEMONITOR_DIR, servicemonitor_chart)
