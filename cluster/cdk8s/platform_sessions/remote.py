"""Staged remote gateway and Blue Shield provider; neither owns the live session yet."""

from __future__ import annotations

import json

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from flux_imagepolicy_crds.io.fluxcd.toolkit.image import (
    ImagePolicySpecFilterTags,
    ImagePolicySpecImageRepositoryRef,
    ImagePolicySpecPolicy,
    ImagePolicySpecPolicyAlphabetical,
    ImagePolicySpecPolicyAlphabeticalOrder,
)
from flux_imagerepository_crds.io.fluxcd.toolkit.image import ImageRepositorySpecSecretRef
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecSelector,
)

from cluster.cdk8s import pod_policy
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.platform_sessions import config
from cluster.cdk8s.providers.cert_manager.certificate import Certificate
from cluster.cdk8s.providers.flux.image_policy import ImagePolicy
from cluster.cdk8s.providers.flux.image_repository import ImageRepository
from cluster.cdk8s.providers.prometheus_operator.pod_monitor import PodMonitor

NAMESPACE = "platform-sessions"
NAME = "platform-sessions-remote"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
GATEWAY = "platform-sessions-gateway"
BLUESHIELD = "platform-sessions-blueshield"
REGISTRY = "git.allegedly.works/ducktape-ci"
_GW_IMAGE = f"{REGISTRY}/{GATEWAY}:unset"
_PROVIDER_IMAGE = f"{REGISTRY}/{BLUESHIELD}:unset"
_PROVIDER_DNS = f"{BLUESHIELD}.{NAMESPACE}.svc"
_GATEWAY_DNS = f"{GATEWAY}.{NAMESPACE}.svc"
_TRUST = "/var/run/provider-trust/ca.crt"
_TOKEN_DIR = "/var/run/provider-credentials"

# A separate ConfigMap prevents preparing the new deployment from rolling the live service.
CONFIG = ConfigMapArgs(
    name="platform-sessions-remote-config",
    namespace=NAMESPACE,
    data={
        "gateway-integrations.json": json.dumps({"integrations": [{"id": "blueshield", "type": "blueshield"}]}),
        "provider-integrations.json": json.dumps(
            {"integrations": [item for item in config.INTEGRATIONS["integrations"] if item["id"] == "blueshield"]}
        ),
        "bearers.json": json.dumps({key: config.BEARERS[key] for key in ("owner", "blueshield-read")}),
        "connections.json": json.dumps(
            {
                "blueshield": {
                    "origin": f"https://{_PROVIDER_DNS}:8443",
                    "control_target": f"{_PROVIDER_DNS}:8444",
                    "control_token_file": f"{_TOKEN_DIR}/control",
                    "owner_token_file": f"{_TOKEN_DIR}/owner",
                    "route_token_file": f"{_TOKEN_DIR}/route",
                    "control_ca_file": _TRUST,
                    "http_ca_file": _TRUST,
                }
            }
        ),
    },
)


def _secret_projection(name: str, path: str) -> k8s.VolumeProjection:
    return k8s.VolumeProjection(secret=k8s.SecretProjection(name=name, items=[k8s.KeyToPath(key="token", path=path)]))


def _security() -> k8s.PodSecurityContext:
    return k8s.PodSecurityContext(
        run_as_non_root=True,
        run_as_user=1000,
        run_as_group=1000,
        fs_group=1000,
        fs_group_change_policy="OnRootMismatch",
        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
    )


def _container_security() -> k8s.SecurityContext:
    return k8s.SecurityContext(
        allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"]), read_only_root_filesystem=True
    )


def _resources() -> k8s.ResourceRequirements:
    return k8s.ResourceRequirements(
        requests={"cpu": k8s.Quantity.from_string("50m"), "memory": k8s.Quantity.from_string("128Mi")},
        limits={"cpu": k8s.Quantity.from_string("1"), "memory": k8s.Quantity.from_string("1Gi")},
    )


def _deployment(
    chart: Chart,
    name: str,
    container: k8s.Container,
    volumes: list[k8s.Volume],
    *,
    node_selector: dict[str, str] | None = None,
) -> None:
    deployment = k8s.KubeDeployment(
        chart,
        f"{name}-deployment",
        metadata=k8s.ObjectMeta(name=name, namespace=NAMESPACE),
        spec=k8s.DeploymentSpec(
            # The legacy service owns all real sessions until an explicit, verified handoff.
            replicas=0,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels={"app": name}),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels={"app": name}),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name="forgejo-images-creds")],
                    node_selector=node_selector,
                    security_context=_security(),
                    containers=[container],
                    volumes=volumes,
                ),
            ),
        ),
    )
    pod_policy.harden(deployment)


def _config_env(name: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(config_map_key_ref=k8s.ConfigMapKeySelector(name=CONFIG.name, key=key))
    )


def _service(chart: Chart, name: str, ports: list[k8s.ServicePort]) -> None:
    k8s.KubeService(
        chart,
        f"{name}-service",
        metadata=k8s.ObjectMeta(name=name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector={"app": name}, ports=ports),
    )


def add_resources(chart: Chart) -> None:
    for role in ("control", "owner", "route"):
        mint_bearer_secret(
            chart,
            f"{BLUESHIELD}-{role}",
            name=f"{BLUESHIELD}-{role}",
            namespace=NAMESPACE,
            key="token",
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
            deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
            immutable=True,
        )

    for name, hostname in ((BLUESHIELD, _PROVIDER_DNS), (GATEWAY, _GATEWAY_DNS)):
        Certificate(
            chart,
            f"{name}-certificate",
            metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
            secret_name=f"{name}-tls",
            issuer_ref=cluster_ca.INTERNAL_ISSUER,
            dns_names=[hostname],
        )

    # Provider images are built by gaffer-private and use main-* tags.
    repository = ImageRepository(
        chart,
        f"{BLUESHIELD}-image-repository",
        metadata=ApiObjectMetadata(name=BLUESHIELD, namespace="flux-system"),
        image=f"{REGISTRY}/{BLUESHIELD}",
        interval="5m",
        secret_ref=ImageRepositorySpecSecretRef(name="forgejo-images-creds"),
    )
    ImagePolicy(
        chart,
        f"{BLUESHIELD}-image-policy",
        metadata=ApiObjectMetadata(name=BLUESHIELD, namespace="flux-system"),
        image_repository_ref=ImagePolicySpecImageRepositoryRef(name=repository.name),
        filter_tags=ImagePolicySpecFilterTags(pattern=r"^main-\d{14}-[0-9a-f]{7}$"),
        policy=ImagePolicySpecPolicy(
            alphabetical=ImagePolicySpecPolicyAlphabetical(order=ImagePolicySpecPolicyAlphabeticalOrder.ASC)
        ),
    )

    k8s.KubePersistentVolumeClaim(
        chart,
        f"{BLUESHIELD}-claim",
        metadata=k8s.ObjectMeta(
            name=BLUESHIELD, namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"}
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="seaweedfs-ovh-ssd",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("1Gi")}),
        ),
    )
    provider_credentials = k8s.Volume(
        name="provider-credentials",
        projected=k8s.ProjectedVolumeSource(
            default_mode=0o440,
            sources=[_secret_projection(f"{BLUESHIELD}-{role}", role) for role in ("control", "owner", "route")],
        ),
    )
    config_volume = k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=CONFIG.name))
    tmp = k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource())
    provider_health = k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", scheme="HTTPS", port=k8s.IntOrString.from_string("https"))
    )
    _deployment(
        chart,
        BLUESHIELD,
        k8s.Container(
            name="provider",
            image=_PROVIDER_IMAGE,
            args=[
                "--integration-config",
                "/etc/platform-sessions/provider-integrations.json",
                "--instance-id",
                "blueshield",
                "--control-address",
                "0.0.0.0:8444",
                "--control-token-file",
                f"{_TOKEN_DIR}/control",
                "--owner-token-file",
                f"{_TOKEN_DIR}/owner",
                "--route-token-file",
                f"{_TOKEN_DIR}/route",
                "--http-port",
                "8443",
                "--metrics-port",
                "9091",
                "--tls-cert",
                "/var/run/provider-tls/tls.crt",
                "--tls-key",
                "/var/run/provider-tls/tls.key",
            ],
            env=[k8s.EnvVar(name="HOME", value="/tmp")],
            ports=[
                k8s.ContainerPort(name="https", container_port=8443),
                k8s.ContainerPort(name="grpc", container_port=8444),
                k8s.ContainerPort(name="metrics", container_port=9091),
            ],
            readiness_probe=provider_health,
            liveness_probe=provider_health,
            resources=_resources(),
            security_context=_container_security(),
            volume_mounts=[
                k8s.VolumeMount(name="config", mount_path="/etc/platform-sessions", read_only=True),
                k8s.VolumeMount(name="provider-credentials", mount_path=_TOKEN_DIR, read_only=True),
                k8s.VolumeMount(name="tls", mount_path="/var/run/provider-tls", read_only=True),
                k8s.VolumeMount(name="data", mount_path="/data"),
                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
            ],
        ),
        [
            config_volume,
            provider_credentials,
            k8s.Volume(name="tls", secret=k8s.SecretVolumeSource(secret_name=f"{BLUESHIELD}-tls")),
            k8s.Volume(
                name="data", persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=BLUESHIELD)
            ),
            tmp,
        ],
        node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
    )
    _service(
        chart,
        BLUESHIELD,
        [
            k8s.ServicePort(name="https", port=8443, target_port=k8s.IntOrString.from_string("https")),
            k8s.ServicePort(name="grpc", port=8444, target_port=k8s.IntOrString.from_string("grpc")),
        ],
    )

    PodMonitor(
        chart,
        f"{BLUESHIELD}-monitor",
        metadata=ApiObjectMetadata(name=BLUESHIELD, namespace=NAMESPACE),
        selector=PodMonitorSpecSelector(match_labels={"app": BLUESHIELD}),
        pod_metrics_endpoints=[PodMonitorSpecPodMetricsEndpoints(port="metrics", path="/metrics", interval="1m")],
    )

    gateway_health = k8s.Probe(http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string("http")))
    _deployment(
        chart,
        GATEWAY,
        k8s.Container(
            name="gateway",
            image=_GW_IMAGE,
            env_from=[k8s.EnvFromSource(secret_ref=k8s.SecretEnvSource(name="platform-sessions-oidc"))],
            env=[
                k8s.EnvVar(name="PLATFORM_SESSIONS_PUBLIC_BASE_URL", value="https://platform-sessions.allegedly.works"),
                _config_env("PLATFORM_SESSIONS_INTEGRATION_CONFIG", "gateway-integrations.json"),
                _config_env("PLATFORM_SESSIONS_BEARERS", "bearers.json"),
                _config_env("PLATFORM_SESSIONS_PROVIDER_CONNECTIONS", "connections.json"),
                k8s.EnvVar(name="PLATFORM_SESSIONS_GRPC_ADDRESS", value="0.0.0.0:8444"),
                k8s.EnvVar(name="PLATFORM_SESSIONS_GRPC_OWNER_TOKEN_FILE", value="/var/run/platform-sessions/owner"),
                k8s.EnvVar(name="PLATFORM_SESSIONS_GRPC_TLS_CERT", value="/var/run/gateway-tls/tls.crt"),
                k8s.EnvVar(name="PLATFORM_SESSIONS_GRPC_TLS_KEY", value="/var/run/gateway-tls/tls.key"),
            ],
            ports=[
                k8s.ContainerPort(name="http", container_port=8080),
                k8s.ContainerPort(name="grpc", container_port=8444),
            ],
            readiness_probe=gateway_health,
            liveness_probe=gateway_health,
            resources=_resources(),
            security_context=_container_security(),
            volume_mounts=[
                k8s.VolumeMount(name="provider-credentials", mount_path=_TOKEN_DIR, read_only=True),
                k8s.VolumeMount(name="bearers", mount_path="/var/run/platform-sessions", read_only=True),
                k8s.VolumeMount(name="trust", mount_path="/var/run/provider-trust", read_only=True),
                k8s.VolumeMount(name="tls", mount_path="/var/run/gateway-tls", read_only=True),
            ],
        ),
        [
            provider_credentials,
            k8s.Volume(
                name="bearers",
                projected=k8s.ProjectedVolumeSource(
                    default_mode=0o440,
                    sources=[
                        _secret_projection(f"platform-sessions-{role}-bearer", role)
                        for role in ("owner", "blueshield-read")
                    ],
                ),
            ),
            k8s.Volume(
                name="trust",
                config_map=k8s.ConfigMapVolumeSource(
                    name=cluster_ca.BUNDLE, items=[k8s.KeyToPath(key=cluster_ca.BUNDLE_KEY, path="ca.crt")]
                ),
            ),
            k8s.Volume(name="tls", secret=k8s.SecretVolumeSource(secret_name=f"{GATEWAY}-tls")),
        ],
    )
    _service(
        chart,
        GATEWAY,
        [
            k8s.ServicePort(name="http", port=8080, target_port=k8s.IntOrString.from_string("http")),
            k8s.ServicePort(name="grpc", port=8444, target_port=k8s.IntOrString.from_string("grpc")),
        ],
    )


def chart(app: App) -> Chart:
    result = Chart(app, NAME, disable_resource_name_hashes=True)
    add_resources(result)
    return result


def platform_sessions_remote(
    chart: Chart,
    directory: RenderedDirectory,
    legacy: Kustomization,
    cert_manager_environment: Kustomization,
    image_automation: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        description="Staged remote session gateway and provider; scale only after a verified ownership handoff.",
        suspend=False,
        wait=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(legacy, cert_manager_environment, image_automation),
    )
