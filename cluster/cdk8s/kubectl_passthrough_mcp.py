"""kubectl-passthrough-mcp (cluster/generated/agents/kubectl-passthrough-mcp/app):
containers/kubernetes-mcp-server in OAuth passthrough mode, its public route, and the
ClusterRoleBinding that makes agentydragon's passthrough identity cluster-admin.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "kubectl-passthrough-mcp"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/kubectl-passthrough-mcp/app"
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAME, labels=(("app.kubernetes.io/name", NAME),)),
)
_CONFIG_MAP = "kubectl-passthrough-mcp-public"
_CONFIG_FILE = "00-public.toml"
_CONFIG_DIR = "/etc/kubectl-passthrough-mcp"
_PUBLIC_CONFIG = f"""\
require_oauth = true
authorization_url = "https://auth.allegedly.works/application/o/kubectl-passthrough-mcp/"
oauth_audience = "kubectl-passthrough-mcp"
# Passthrough forwards the caller's JWT as-is to kube-apiserver. No STS
# credentials needed because there's no token exchange on this server.
cluster_auth_mode = "passthrough"
cluster_provider_strategy = "in-cluster"
server_url = "https://kubectl-passthrough-mcp.allegedly.works"
port = "{SERVICE.pod_port}"
"""


def _healthz(*, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(SERVICE.pod_port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _deployment(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAME,
            labels=SERVICE.pods.selector,
            annotations={
                "description": (
                    "containers/kubernetes-mcp-server in OAuth passthrough mode. Caller's Authentik JWT is"
                    " forwarded directly to kube-apiserver; server itself is unprivileged."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    service_account_name=NAME,
                    # The in-cluster provider builds its client with client-go's InClusterConfig,
                    # which reads this token and CA before passthrough swaps in the caller's JWT.
                    automount_service_account_token=True,
                    containers=[
                        k8s.Container(
                            name="server",
                            image="ghcr.io/containers/kubernetes-mcp-server:v0.0.66",
                            image_pull_policy="IfNotPresent",
                            # Config is public-only — the OAuth2 provider is public (PKCE),
                            # and passthrough mode does no token exchange, so no secrets needed.
                            args=[f"--config={_CONFIG_DIR}/{_CONFIG_FILE}"],
                            ports=[SERVICE.port.k8s_container_port()],
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="public",
                                    mount_path=f"{_CONFIG_DIR}/{_CONFIG_FILE}",
                                    sub_path=_CONFIG_FILE,
                                    read_only=True,
                                )
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                limits={
                                    "memory": k8s.Quantity.from_string("256Mi"),
                                    "cpu": k8s.Quantity.from_string("200m"),
                                },
                            ),
                            readiness_probe=_healthz(initial_delay_seconds=3, period_seconds=10),
                            liveness_probe=_healthz(initial_delay_seconds=15, period_seconds=20),
                        )
                    ],
                    volumes=[k8s.Volume(name="public", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP))],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAME, vpa=Vpa.DISABLED)
    k8s.KubeServiceAccount(
        chart,
        "serviceaccount",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAME,
            annotations={
                "description": (
                    "Service account for kubectl-passthrough-mcp pod. No k8s permissions — server uses"
                    " passthrough OAuth token for kube-apiserver calls."
                )
            },
        ),
    )
    k8s.KubeConfigMap(
        chart,
        "configmap",
        metadata=k8s.ObjectMeta(name=_CONFIG_MAP, namespace=NAME),
        data={_CONFIG_FILE: _PUBLIC_CONFIG},
    )
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAME),
        spec=k8s.ServiceSpec(selector=SERVICE.pods.selector, ports=[SERVICE.port.k8s_service_port()], type="ClusterIP"),
    )
    # NOT behind the Authentik outpost — the MCP server drives the full OAuth dance
    # (DCR, PKCE, resource metadata) internally using containers/kubernetes-mcp-server's
    # built-in OAuth2/OIDC support.
    https_route(
        chart,
        "httproute",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAME),
        hostnames=["kubectl-passthrough-mcp.allegedly.works"],
        backend=SERVICE,
        timeout="60s",
        hsts=False,
        listener=None,
    )
    # Grants agentydragon's own identity cluster-admin when authenticated through
    # kubectl-passthrough-mcp (username prefix oidc-ksbx:, distinct from Headlamp's
    # oidc: prefix — see cluster/terraform/main/infrastructure.tf's AuthenticationConfiguration).
    # Mirrors headlamp.py's oidc-agentydragon-admin,
    # scoped to this issuer instead. Consumed by agentplane-staging's `kubernetes_admin`
    # ActionGroup, which links the operator's passthrough identity (cluster/cdk8s/agentplane/
    # staging.py): an agent requests an Action, agentydragon approves it, and the call executes
    # with agentydragon's own passthrough identity — the approval click is the only gate, by design.
    k8s.KubeClusterRoleBinding(
        chart,
        "agentydragon-admin",
        metadata=k8s.ObjectMeta(name="oidc-ksbx-agentydragon-admin"),
        subjects=[k8s.Subject(kind="User", name="oidc-ksbx:agentydragon", api_group="rbac.authorization.k8s.io")],
        role_ref=k8s.RoleRef(kind="ClusterRole", name="cluster-admin", api_group="rbac.authorization.k8s.io"),
    )
    return chart


def kubectl_passthrough_mcp(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, suspend=False, timeout="5m")
