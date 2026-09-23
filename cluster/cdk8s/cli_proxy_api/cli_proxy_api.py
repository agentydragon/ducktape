"""CLIProxyAPI: Anthropic Messages (/v1/messages) <-> ChatGPT/Codex subscription backend, with
tool-call translation (function_call -> tool_use). Backs the `codex-claude` wrapper and owns the
Claude/Codex OAuth sessions used by the CLIProxyAPI integration.

The image tag is the placeholder "unset"; the hand-written
cluster/k8s/cli-proxy-api/image-pins/kustomization.yaml overrides it at `kustomize build` time via
Flux's image-automation marker. The client and management key Secrets stay hand-written SOPS
files beside the generated output.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
)
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesMatches,
    HttpRouteSpecRulesMatchesPath,
    HttpRouteSpecRulesMatchesPathType,
    HttpRouteSpecRulesTimeouts,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import cilium
from cluster.cdk8s.external_secrets.external_secret import add_external_secret, cluster_secret_store, remote_data
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.forgejo_images import SECRET_NAME
from cluster.cdk8s.gateway import cluster_gateway_parent_ref, https_route
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/cli-proxy-api"
_NAME = "cli-proxy-api"
_NAMESPACE = "cli-proxy-api"
_LABELS = {"app.kubernetes.io/name": _NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/cli-proxy-api:unset"
_PORT = 8317
_CONFIG_SECRET = "cli-proxy-api-config"
_DATA_CLAIM = "cli-proxy-api-data"
_ADMIN_OIDC_SECRET = "cli-proxy-api-admin-oidc"
_KEY_FILES = ("client-key.sops.yaml", "management-key.sops.yaml")

_CONFIG = textwrap.dedent(
    """\
    host: "0.0.0.0"
    port: 8317
    auth-dir: "/data/auth"
    api-keys:
      - "{{ .client_key }}"
    debug: false
    # Recheck restored provider quota instead of honoring stale local cooldown windows.
    disable-cooling: true
    streaming:
      bootstrap-retries: 3
    # Permit AIQuota's management-key requests from its separate pod.
    # Browser management access uses native OIDC (deployment.yaml).
    remote-management:
      allow-remote: true
    """
)


def _namespace(scope: Construct) -> None:
    k8s.KubeNamespace(
        scope,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=_NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "auto",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
        ),
    )


def _data_claim(scope: Construct) -> None:
    # Holds the Claude and Codex OAuth auth files that CLIProxyAPI refreshes in place.
    # Single replica (Recreate strategy) because refresh tokens are rotated by this one writer.
    k8s.KubePersistentVolumeClaim(
        scope,
        "data",
        metadata=k8s.ObjectMeta(name=_DATA_CLAIM, namespace=_NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="seaweedfs-ovh",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("1Gi")}),
        ),
    )


def _config(scope: Construct) -> None:
    # CLIProxyAPI requires a single config file. ESO renders the API key from the
    # SOPS-managed client-key Secret, so this template remains safe to review and edit.
    add_external_secret(
        scope,
        "config",
        name=_CONFIG_SECRET,
        namespace=_NAMESPACE,
        refresh="1h",
        store=cluster_secret_store("kubernetes-cli-proxy-api-secret-store"),
        data=[remote_data("cli-proxy-api-client-key", "client-key", secret_key="client_key")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2, data={"config.yaml": _CONFIG}
        ),
        annotations={
            "description": (
                "CLIProxyAPI config.yaml rendered from the client-key Secret. Retry an upstream stream up "
                "to three times only before its first response byte reaches the caller. Remote management "
                "uses native Authentik OIDC for browsers and a management key for AIQuota."
            )
        },
    )


def _secret_env(name: str, secret: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=secret, key=key))
    )


def _deployment(scope: Construct) -> None:
    tcp_probe = k8s.TcpSocketAction(port=k8s.IntOrString.from_number(_PORT))
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME, namespace=_NAMESPACE, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            # Recreate: single owner of the refresh-rotated OAuth tokens on the PVC.
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    # No explicit zone nodeSelector: the PVC's seaweedfs-ovh StorageClass already
                    # pins scheduling to topology.kubernetes.io/zone=hil-ovh via WaitForFirstConsumer
                    # + allowedTopologies (seaweedfs_csi/driver.py), and the
                    # already-bound PV carries that same zone in its own nodeAffinity.
                    # hil-ovh's only schedulable workers (ovh-ns103711, ovh-ns102453) run hot enough
                    # that the descheduler's LowNodeUtilization plugin (cluster/cdk8s/descheduler.py)
                    # evicts this pod roughly every 15 minutes for node overutilization. As the
                    # single-writer, Recreate-strategy holder of the Codex OAuth refresh token on its
                    # PVC, it can't tolerate that churn — each eviction breaks the auto-refresh worker's
                    # session. Allow the zone's otherwise-idle control-plane nodes as real overflow
                    # capacity instead of only ever bouncing between the two contended workers.
                    tolerations=[
                        k8s.Toleration(
                            key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                        )
                    ],
                    init_containers=[
                        k8s.Container(
                            name="init-auth-dir",
                            image="busybox:1.38",
                            command=["sh", "-c", "mkdir -p /data/auth"],
                            volume_mounts=[k8s.VolumeMount(name="data", mount_path="/data")],
                        )
                    ],
                    containers=[
                        k8s.Container(
                            name=_NAME,
                            image=_IMAGE,
                            args=["-config", "/config/config.yaml"],
                            env=[
                                _secret_env("MANAGEMENT_PASSWORD", "cli-proxy-api-management", "management-password"),
                                *(
                                    _secret_env(key, _ADMIN_OIDC_SECRET, key)
                                    for key in (
                                        "MANAGEMENT_OIDC_ISSUER",
                                        "MANAGEMENT_OIDC_CLIENT_ID",
                                        "MANAGEMENT_OIDC_CLIENT_SECRET",
                                        "MANAGEMENT_OIDC_REDIRECT_URL",
                                        "MANAGEMENT_OIDC_ALLOWED_SUBJECTS",
                                    )
                                ),
                            ],
                            ports=[k8s.ContainerPort(name="http", container_port=_PORT, protocol="TCP")],
                            readiness_probe=k8s.Probe(tcp_socket=tcp_probe, initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=k8s.Probe(tcp_socket=tcp_probe, initial_delay_seconds=30, period_seconds=30),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("1"),
                                    "memory": k8s.Quantity.from_string("512Mi"),
                                },
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/config", read_only=True),
                                k8s.VolumeMount(name="data", mount_path="/data"),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", secret=k8s.SecretVolumeSource(secret_name=_CONFIG_SECRET)),
                        k8s.Volume(
                            name="data",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_DATA_CLAIM),
                        ),
                    ],
                ),
            ),
        ),
    )


def _service(scope: Construct) -> None:
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(
                    name="http", port=_PORT, target_port=k8s.IntOrString.from_string("http"), protocol="TCP"
                )
            ],
        ),
    )


def _routes(scope: Construct) -> None:
    HttpRoute(
        scope,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref()],
            hostnames=["cli-proxy-api.allegedly.works"],
            rules=[
                # CLIProxyAPI streams model responses (incl. long Codex reasoning); allow long requests.
                HttpRouteSpecRules(
                    timeouts=HttpRouteSpecRulesTimeouts(request="600s", backend_request="600s"),
                    matches=[
                        HttpRouteSpecRulesMatches(
                            path=HttpRouteSpecRulesMatchesPath(
                                type=HttpRouteSpecRulesMatchesPathType.PATH_PREFIX, value="/v1"
                            )
                        )
                    ],
                    backend_refs=[HttpRouteSpecRulesBackendRefs(name=_NAME, port=_PORT)],
                )
            ],
        ),
    )
    https_route(
        scope,
        "admin-route",
        metadata=metadata("cli-proxy-api-admin", _NAMESPACE),
        hostname="cli-proxy-api-admin.allegedly.works",
        backend=_NAME,
        port=_PORT,
        hsts=False,
        listener=None,
    )


def _network_policy(scope: Construct) -> None:
    # Gateway, LiteLLM, and AIQuota reach CLIProxyAPI; the backend authenticates requests.
    cilium.network_policy(
        scope,
        "network-policy",
        metadata=metadata("cli-proxy-api-ingress", _NAMESPACE),
        selector=_LABELS,
        ingress=[
            # cilium-envoy hostNetwork traffic carries reserved:ingress identity. Preserves the
            # existing cli-proxy-api.allegedly.works /v1 HTTPRoute, which routes straight to this
            # Service, unauthenticated, for LiteLLM's model traffic.
            cilium.ingress_from_gateway(_PORT),
            # LiteLLM's codex-*/chatgpt-* upstreams (litellm/config.py) call the
            # in-cluster Service by cluster DNS, not through the Gateway.
            cilium.ingress_from(cilium.endpoint_labels("litellm", "litellm"), ports=[_PORT]),
            # aiquota retrieves Claude and Codex subscription usage through the authenticated
            # CLIProxyAPI management endpoint.
            cilium.ingress_from(cilium.endpoint_labels(_NAMESPACE, "aiquota"), ports=[_PORT]),
            # Kubelet readiness/liveness tcpSocket probes originate from the node host.
            CiliumNetworkPolicySpecIngress(
                from_entities=[CiliumNetworkPolicySpecIngressFromEntities.HOST],
                to_ports=[
                    CiliumNetworkPolicySpecIngressToPorts(
                        ports=[
                            CiliumNetworkPolicySpecIngressToPortsPorts(
                                port=str(_PORT), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                            )
                        ]
                    )
                ],
            ),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _data_claim(chart)
    _config(chart)
    _deployment(chart)
    _service(chart)
    _routes(chart)
    _network_policy(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml", *_KEY_FILES], components=["./image-pins"]),
    )


def cli_proxy_api(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_config: Kustomization,
    gateway: Kustomization,
    cert_manager_environment: Kustomization,
    sso_providers_tf: Kustomization,
    forgejo_images: Kustomization,
) -> Kustomization:
    name = "cli-proxy-api"
    return flux_kustomization(
        chart,
        name,
        artifact,
        retry_interval=None,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_config, gateway, cert_manager_environment, sso_providers_tf, forgejo_images
        ),
    )
