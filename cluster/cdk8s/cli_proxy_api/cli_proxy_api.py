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

from cdk8s import ApiObjectMetadata, App, Chart
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

from cluster.cdk8s import cilium, namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry.chart import SECRET_NAME
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/cli-proxy-api"
NAME = "cli-proxy-api"
NAMESPACE = "cli-proxy-api"
_IMAGE = "git.allegedly.works/ducktape-ci/cli-proxy-api:unset"
PORT = 8317
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=PORT),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
_CONFIG_SECRET = "cli-proxy-api-config"
_DATA_CLAIM = "cli-proxy-api-data"
_ADMIN_OIDC = SecretRef(namespace=NAMESPACE, name="cli-proxy-api-admin-oidc")
# The SOPS-managed management key; aiquota reads it too.
MANAGEMENT_PASSWORD = SecretRef(namespace=NAMESPACE, name="cli-proxy-api-management").key("management-password")
KEY_FILES = ("client-key.sops.yaml", "management-key.sops.yaml")

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
    namespaces.namespace(scope, "namespace", name=NAMESPACE, vpa=Vpa.AUTO)


def _data_claim(scope: Construct) -> None:
    # Holds the Claude and Codex OAuth auth files that CLIProxyAPI refreshes in place.
    # Single replica (Recreate strategy) because refresh tokens are rotated by this one writer.
    k8s.KubePersistentVolumeClaim(
        scope,
        "data",
        metadata=k8s.ObjectMeta(name=_DATA_CLAIM, namespace=NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="seaweedfs-ovh",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("1Gi")}),
        ),
    )


def _config(scope: Construct) -> None:
    # CLIProxyAPI requires a single config file. ESO renders the API key from the
    # SOPS-managed client-key Secret, so this template remains safe to review and edit.
    ExternalSecret(
        scope,
        "config",
        metadata=ApiObjectMetadata(
            name=_CONFIG_SECRET,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "CLIProxyAPI config.yaml rendered from the client-key Secret. Retry an upstream stream up "
                    "to three times only before its first response byte reaches the caller. Remote management "
                    "uses native Authentik OIDC for browsers and a management key for AIQuota."
                )
            },
        ),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster("kubernetes-cli-proxy-api-secret-store"),
        data=[remote_data("cli-proxy-api-client-key", "client-key", secret_key="client_key")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2, data={"config.yaml": _CONFIG}
        ),
    )


def _deployment(scope: Construct) -> None:
    tcp_probe = k8s.TcpSocketAction(port=k8s.IntOrString.from_number(SERVICE.pod_port))
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=SERVICE.pods.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            # Recreate: single owner of the refresh-rotated OAuth tokens on the PVC.
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=SERVICE.pods.selector),
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
                    tolerations=[node_scheduling.CONTROL_PLANE_TOLERATION],
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
                            name=NAME,
                            image=_IMAGE,
                            args=["-config", "/config/config.yaml"],
                            env=[
                                MANAGEMENT_PASSWORD.env_var("MANAGEMENT_PASSWORD"),
                                *(
                                    _ADMIN_OIDC.key(key).env_var(key)
                                    for key in (
                                        "MANAGEMENT_OIDC_ISSUER",
                                        "MANAGEMENT_OIDC_CLIENT_ID",
                                        "MANAGEMENT_OIDC_CLIENT_SECRET",
                                        "MANAGEMENT_OIDC_REDIRECT_URL",
                                        "MANAGEMENT_OIDC_ALLOWED_SUBJECTS",
                                    )
                                ),
                            ],
                            ports=[SERVICE.port.k8s_container_port()],
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
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector=SERVICE.pods.selector, ports=[SERVICE.port.k8s_service_port()]),
    )


def _routes(scope: Construct) -> None:
    # CLIProxyAPI streams model responses (incl. long Codex reasoning); allow long requests.
    https_route(
        scope,
        "route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=["cli-proxy-api.allegedly.works"],
        backend=SERVICE,
        path_prefix="/v1",
        timeout="600s",
        hsts=False,
        listener=None,
    )
    https_route(
        scope,
        "admin-route",
        metadata=ApiObjectMetadata(name="admin", namespace=NAMESPACE),
        hostnames=["cli-proxy-api-admin.allegedly.works"],
        backend=SERVICE,
        hsts=False,
        listener=None,
    )


def _network_policy(scope: Construct) -> None:
    # Gateway, LiteLLM, and AIQuota reach CLIProxyAPI; the backend authenticates requests.
    NetworkPolicy(
        scope,
        "network-policy",
        metadata=ApiObjectMetadata(name="ingress", namespace=NAMESPACE),
        endpoint_selector=SERVICE.pods.selector,
        ingress=[
            # cilium-envoy hostNetwork traffic carries reserved:ingress identity. Preserves the
            # existing cli-proxy-api.allegedly.works /v1 HTTPRoute, which routes straight to this
            # Service, unauthenticated, for LiteLLM's model traffic.
            IngressRule.from_gateway(SERVICE.pod_port),
            # LiteLLM's codex-*/chatgpt-* upstreams (litellm/config.py) call the
            # in-cluster Service by cluster DNS, not through the Gateway.
            IngressRule.from_endpoints(cilium.endpoint_labels("litellm", "litellm"), ports=[SERVICE.pod_port]),
            # aiquota retrieves Claude and Codex subscription usage through the authenticated
            # CLIProxyAPI management endpoint.
            IngressRule.from_endpoints(cilium.endpoint_labels(NAMESPACE, "aiquota"), ports=[SERVICE.pod_port]),
            # Kubelet readiness/liveness tcpSocket probes originate from the node host.
            CiliumNetworkPolicySpecIngress(
                from_entities=[CiliumNetworkPolicySpecIngressFromEntities.HOST],
                to_ports=[
                    CiliumNetworkPolicySpecIngressToPorts(
                        ports=[
                            CiliumNetworkPolicySpecIngressToPortsPorts(
                                port=str(SERVICE.pod_port),
                                protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP,
                            )
                        ]
                    )
                ],
            ),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _data_claim(chart)
    _config(chart)
    _deployment(chart)
    _service(chart)
    _routes(chart)
    _network_policy(chart)
    return chart


def cli_proxy_api(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization, kyverno: Kustomization
) -> Kustomization:
    name = "cli-proxy-api"
    return flux_kustomization(
        chart,
        name,
        directory,
        retry_interval=None,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_operator,
            # Kyverno's failurePolicy: Fail webhooks admit the Deployment, HTTPRoute and Namespace.
            kyverno,
        ),
    )
