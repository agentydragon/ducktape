"""kubectl-passthrough-mcp (cluster/k8s/agents/kubectl-passthrough-mcp/app):
containers/kubernetes-mcp-server in OAuth passthrough mode, its public route, and the
ClusterRoleBinding that makes agentydragon's passthrough identity cluster-admin.
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

NAME = "kubectl-passthrough-mcp"
OUTPUT_DIR = "cluster/k8s/agents/kubectl-passthrough-mcp/app"
_LABELS = {"app.kubernetes.io/name": NAME}
_PORT = 8080
_CONFIG_MAP = "kubectl-passthrough-mcp-public"
_CONFIG_FILE = "00-public.toml"
_CONFIG_DIR = "/etc/kubectl-passthrough-mcp"
_PUBLIC_CONFIG = """\
require_oauth = true
authorization_url = "https://auth.allegedly.works/application/o/kubectl-passthrough-mcp/"
oauth_audience = "kubectl-passthrough-mcp"
# Passthrough forwards the caller's JWT as-is to kube-apiserver. No STS
# credentials needed because there's no token exchange on this server.
cluster_auth_mode = "passthrough"
cluster_provider_strategy = "in-cluster"
server_url = "https://kubectl-passthrough-mcp.allegedly.works"
port = "8080"
"""


def _healthz(*, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_PORT)),
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
            labels=_LABELS,
            annotations={
                "description": (
                    "containers/kubernetes-mcp-server in OAuth passthrough mode. Caller's Authentik JWT is"
                    " forwarded directly to kube-apiserver; server itself is unprivileged."
                ),
                "reloader.stakater.com/auto": "true",
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    service_account_name=NAME,
                    containers=[
                        k8s.Container(
                            name="server",
                            image="ghcr.io/containers/kubernetes-mcp-server:v0.0.66",
                            image_pull_policy="IfNotPresent",
                            # Config is public-only — the OAuth2 provider is public (PKCE),
                            # and passthrough mode does no token exchange, so no secrets needed.
                            args=[f"--config={_CONFIG_DIR}/{_CONFIG_FILE}"],
                            ports=[k8s.ContainerPort(name="http", container_port=_PORT, protocol="TCP")],
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
    k8s.KubeNamespace(
        chart, "namespace", metadata=k8s.ObjectMeta(name=NAME, labels={"goldilocks.fairwinds.com/enabled": "false"})
    )
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
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[k8s.ServicePort(name="http", port=_PORT, target_port=k8s.IntOrString.from_string("http"))],
            type="ClusterIP",
        ),
    )
    # NOT behind the Authentik outpost — the MCP server drives the full OAuth dance
    # (DCR, PKCE, resource metadata) internally using containers/kubernetes-mcp-server's
    # built-in OAuth2/OIDC support.
    https_route(
        chart,
        "httproute",
        metadata=metadata(NAME, NAME),
        hostname="kubectl-passthrough-mcp.allegedly.works",
        backend=NAME,
        port=_PORT,
        timeout="60s",
        hsts=False,
        listener=None,
    )
    # Grants agentydragon's own identity cluster-admin when authenticated through
    # kubectl-passthrough-mcp (username prefix oidc-ksbx:, distinct from Headlamp's
    # oidc: prefix — see cluster/terraform/main/infrastructure.tf's AuthenticationConfiguration).
    # Mirrors cluster/k8s/headlamp/clusterrolebinding.yaml's oidc-agentydragon-admin,
    # scoped to this issuer instead. Consumed by haku-console's operator_oauth flow for the
    # kubectl-passthrough-mcp MCP server entry: Haku proposes a call, agentydragon approves in
    # haku-console's trusted UI, and the call executes with agentydragon's own passthrough
    # identity — the approval click is the only gate, by design.
    k8s.KubeClusterRoleBinding(
        chart,
        "agentydragon-admin",
        metadata=k8s.ObjectMeta(name="oidc-ksbx-agentydragon-admin"),
        subjects=[k8s.Subject(kind="User", name="oidc-ksbx:agentydragon", api_group="rbac.authorization.k8s.io")],
        role_ref=k8s.RoleRef(kind="ClusterRole", name="cluster-admin", api_group="rbac.authorization.k8s.io"),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))


def kubectl_passthrough_mcp(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(api_version="apps/v1", kind="Deployment", name=NAME, namespace=NAME)
            ],
        ),
    )
