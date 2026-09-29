"""The Grocy MCP server, rendered into each household's `<household>/mcp`: the Deployment,
Service and ServiceMonitor, pull credentials, the OAuth-state DSN and the public HTTPRoute.

The server's image tag is the placeholder "unset"; the hand-written
`mcp-base/image-pins/kustomization.yaml`, a Component each household's kustomization
includes, overrides it at `kustomize build` time via Flux's image-automation marker
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker).

Hand-written beside the generated output in each household's `mcp/`: the
`kustomization.yaml`, whose configMapGenerator renders `config.yaml`.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.grocy import app as grocy  # `app` is the cdk8s App parameter here
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.mcp_oauth_state import CONSUMER_SECRET, GROCY_SF, GROCY_VALLEJO, add_consumer_credentials
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

# Holds only `image-pins/`, the Component both households' kustomizations include.
BASE_DIR = f"{HAND_WRITTEN_ROOT}/grocy/mcp-base"
_NAME = "grocy-mcp-server"
_LABELS = (("app.kubernetes.io/name", "grocy-mcp"), ("app.kubernetes.io/component", "server"))
_IMAGE = "git.allegedly.works/ducktape-ci/grocy-mcp:unset"
_OAUTH_STORES = {"sf": GROCY_SF, "vallejo": GROCY_VALLEJO}


def _service(household: str) -> ServiceRef:
    return ServiceRef(
        name=_NAME,
        port=Port(name="http", number=8765),
        pods=Pods(namespace=grocy.service(household).pods.namespace, labels=_LABELS),
    )


def _server(scope: Construct, household: str) -> None:
    """The household's server: its Deployment, and the Service and ServiceMonitor in front of it."""
    http = _service(household)
    # Prometheus metrics, cluster-internal only (not on the HTTPRoute).
    metrics = ServiceRef(name=http.name, port=Port(name="metrics", number=9090), pods=http.pods)
    namespace = http.pods.namespace
    # Reflector's copy of the OIDC client Terraform writes into authentik
    # (tf/gitops/agent-machine-access/grocy-<household>.tf).
    oidc = SecretRef(namespace=namespace, name=f"grocy-mcp-oidc-{household}")
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
                                oidc.key("client_id").env_var("GROCY_MCP_AUTH__OIDC_CLIENT_ID"),
                                oidc.key("client_secret").env_var("GROCY_MCP_AUTH__OIDC_CLIENT_SECRET"),
                                oidc.key("grocy_proxy_client_id").env_var("GROCY_MCP_AUTH__PROXY_CLIENT_ID"),
                                SecretRef(namespace=namespace, name=CONSUMER_SECRET)
                                .key("uri")
                                .env_var("GROCY_MCP_PERSISTENCE__URL"),
                                k8s.EnvVar(name="LOG_LEVEL", value="INFO"),
                                # Non-secret structured config (grocy_url, auth issuer/URLs/
                                # direct_jwt_trusts, persistence) comes from this YAML file, rendered
                                # by the household kustomization's grocy-mcp-config configMapGenerator.
                                # Secrets stay in env.
                                k8s.EnvVar(name="GROCY_MCP_CONFIG_FILE", value="/etc/grocy-mcp/config.yaml"),
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
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path="/etc/grocy-mcp", read_only=True)],
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
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name="grocy-mcp-config"))],
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
        hostnames=[f"grocy-mcp-{household}.allegedly.works"],
        backend=_service(household),
        timeout="60s",
        hsts=False,
        listener=None,
    )
    return chart


def write_manifests(root: Path) -> None:
    for household, _display in grocy.HOUSEHOLDS:
        write_charts(root, f"{HAND_WRITTEN_ROOT}/grocy/{household}/mcp", partial(household_chart, household=household))
