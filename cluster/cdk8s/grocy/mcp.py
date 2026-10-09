"""The Grocy MCP server, one directory per household (`<household>/mcp`, which the household's
`app/` directory includes): the Deployment, Service and ServiceMonitor, pull credentials, the
OAuth-state DSN, the public HTTPRoute, and the `grocy-mcp-config` settings file the directory's
`kustomization.yaml` renders.

The server's image tag is the placeholder "unset"; the hand-written `PINS_DIR` Component,
which each household's kustomization includes across the roots, overrides it at
`kustomize build` time via Flux's image-automation marker (cluster/cdk8s/AGENTS.md § the
`:tag` Setters marker).
"""

from __future__ import annotations

import posixpath
from functools import partial
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.authentik import app as authentik  # `app` is the cdk8s App parameter here
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import ConfigMapArgs, kustomize_kustomization
from cluster.cdk8s.forgejo.images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.grocy import app as grocy  # `app` is the cdk8s App parameter here
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.mcp_oauth_state import CONSUMER_SECRET, GROCY_SF, GROCY_VALLEJO, add_consumer_credentials
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretKey, SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from grocy_mcp.mcp_types import CONFIG_FILE_ENV, ServerSettings
from util.settings_contract import env_name, settings_file

PINS_DIR = f"{HAND_WRITTEN_ROOT}/grocy/mcp-image-pins"
_NAME = "grocy-mcp-server"
_LABELS = (("app.kubernetes.io/name", "grocy-mcp"), ("app.kubernetes.io/component", "server"))
_IMAGE = "git.allegedly.works/ducktape-ci/grocy-mcp:unset"
_OAUTH_STORES = {"sf": GROCY_SF, "vallejo": GROCY_VALLEJO}
_CONFIG_MAP = "grocy-mcp-config"
_CONFIG_DIR = "/etc/grocy-mcp"
_CONFIG_FILE = "config.yaml"


def output_dir(household: str) -> str:
    return f"{grocy.ROOT}/{household}/mcp"


def _application(household: str) -> str:
    """The slug of the Authentik application OIDCProxy logs users in against, and the first
    label of the server's public host: tf/gitops/agent-machine-access/grocy-<household>.tf
    registers both."""
    return f"grocy-mcp-{household}"


def _hostname(household: str) -> str:
    return f"{_application(household)}.allegedly.works"


def _service(household: str) -> ServiceRef:
    return ServiceRef(
        name=_NAME,
        port=Port(name="http", number=8765),
        pods=Pods(namespace=grocy.service(household).pods.namespace, labels=_LABELS),
    )


def _oidc(household: str) -> SecretRef:
    """Reflector's copy of the OIDC client Terraform writes into authentik
    (tf/gitops/agent-machine-access/grocy-<household>.tf)."""
    return SecretRef(namespace=_service(household).pods.namespace, name=f"grocy-mcp-oidc-{household}")


def _secret_settings(household: str) -> dict[tuple[str, ...], SecretKey]:
    """The `ServerSettings` fields Secrets supply through env, completing the settings file."""
    oidc = _oidc(household)
    return {
        ("auth", "oidc_client_id"): oidc.key("client_id"),
        ("auth", "oidc_client_secret"): oidc.key("client_secret"),
        ("auth", "proxy_client_id"): oidc.key("grocy_proxy_client_id"),
        ("persistence", "url"): SecretRef(namespace=oidc.namespace, name=CONSUMER_SECRET).key("uri"),
    }


def _config_map(household: str) -> ConfigMapArgs:
    """The settings file, whose content hash in the ConfigMap's name rolls the server on a change."""
    settings = settings_file(
        ServerSettings,
        {
            "grocy_url": f"https://{grocy.hostname(household)}",
            "auth": {
                "oidc_issuer": authentik.oidc_issuer(_application(household)),
                "public_base_url": f"https://{_hostname(household)}",
            },
            "persistence": {"kind": "postgres"},
        },
        supplied=_secret_settings(household).keys(),
    )
    oidc = _oidc(household)
    header = (
        f"# Non-secret config for the {oidc.namespace} MCP server, mounted at {CONFIG_FILE_ENV}.\n"
        f"# Secrets (oidc client_id/secret, proxy client_id) come from env ({oidc.name}\n"
        "# k8s Secret), so no single field here is secret.\n"
    )
    return ConfigMapArgs(
        name=_CONFIG_MAP, namespace=oidc.namespace, literals=[f"{_CONFIG_FILE}={header}{yaml_config(settings)}"]
    )


def _server(scope: Construct, household: str) -> None:
    """The household's server: its Deployment, and the Service and ServiceMonitor in front of it."""
    http = _service(household)
    # Prometheus metrics, cluster-internal only (not on the HTTPRoute).
    metrics = ServiceRef(name=http.name, port=Port(name="metrics", number=9090), pods=http.pods)
    namespace = http.pods.namespace
    probe_port = k8s.IntOrString.from_number(http.pod_port)
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=namespace,
            labels=http.pods.selector,
            annotations={
                "description": (
                    "FastMCP server generating Grocy tools from Grocy's OpenAPI spec. Per-request token"
                    " exchange swaps the caller's Authentik JWT for a Grocy-proxy-scoped JWT before calling"
                    " Grocy."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=http.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=http.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    # Both this workload and its shared CNPG OAuth-state database are pinned to
                    # hil-ovh, avoiding cross-site database traffic.
                    node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
                    # Stateless (config only, no PVC). Allow control-plane nodes as overflow
                    # capacity, but prefer workers to keep ordinary application I/O away from
                    # etcd disks.
                    tolerations=[node_scheduling.CONTROL_PLANE_TOLERATION],
                    affinity=node_scheduling.PREFER_WORKERS,
                    containers=[
                        k8s.Container(
                            name="server",
                            image=_IMAGE,
                            image_pull_policy="Always",
                            ports=[http.port.k8s_container_port(), metrics.port.k8s_container_port()],
                            env=[
                                *(
                                    key.env_var(env_name(ServerSettings, *path))
                                    for path, key in _secret_settings(household).items()
                                ),
                                k8s.EnvVar(name="LOG_LEVEL", value="INFO"),
                                k8s.EnvVar(name=CONFIG_FILE_ENV, value=f"{_CONFIG_DIR}/{_CONFIG_FILE}"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                limits={
                                    # TODO(vpa-memory-audit): 256Mi -> 768Mi. VPA observed 335Mi
                                    # request / 355Mi upper in grocy-sf (256Mi/256Mi in vallejo),
                                    # both at or over the old limit. Both households take this
                                    # limit, so the higher of the two governs.
                                    "memory": k8s.Quantity.from_string("768Mi"),
                                    "cpu": k8s.Quantity.from_string("200m"),
                                },
                            ),
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path=_CONFIG_DIR, read_only=True)],
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=probe_port),
                                initial_delay_seconds=3,
                                period_seconds=10,
                            ),
                            liveness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=probe_port),
                                initial_delay_seconds=15,
                                period_seconds=20,
                            ),
                        )
                    ],
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP))],
                ),
            ),
        ),
    )
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=http.name, namespace=namespace, labels=http.labels),
        spec=k8s.ServiceSpec(
            selector=http.pods.selector,
            ports=[http.port.k8s_service_port(), metrics.port.k8s_service_port()],
            type="ClusterIP",
        ),
    )
    ServiceMonitor(
        scope,
        "servicemonitor",
        metadata=ApiObjectMetadata(name=_NAME, namespace=namespace),
        selector=ServiceMonitorSpecSelector(match_labels=http.labels),
        endpoints=[Endpoint.plain(port=metrics.port.name, scrape_timeout="10s")],
    )


def household_chart(app: App, *, household: str) -> Chart:
    namespace = _service(household).pods.namespace
    chart = Chart(app, f"grocy-mcp-{household}", disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=namespace)
    add_consumer_credentials(chart, _OAUTH_STORES[household])
    _server(chart, household)
    # NOT behind the Authentik outpost — OIDCProxy runs inside the pod and drives the full MCP
    # OAuth dance (DCR, PKCE, resource metadata).
    https_route(
        chart,
        "httproute",
        metadata=ApiObjectMetadata(name=f"grocy-mcp-{household}-server", namespace=namespace),
        hostnames=[_hostname(household)],
        backend=_service(household),
        timeout="60s",
        hsts=False,
        listener=None,
    )
    return chart


def write_manifests(root: Path) -> None:
    for household in grocy.HOUSEHOLDS:
        directory = output_dir(household)
        write_yaml(
            root / directory / "kustomization.yaml",
            kustomize_kustomization(
                resources=[write_charts(root, directory, partial(household_chart, household=household))],
                components=[posixpath.relpath(PINS_DIR, directory)],
                config_map_generator=[_config_map(household)],
            ),
        )
