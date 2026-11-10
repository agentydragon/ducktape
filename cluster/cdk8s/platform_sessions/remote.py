"""Gateway and separate providers for Platform Sessions."""

from __future__ import annotations

import json
from pathlib import Path

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
from cluster.cdk8s.cert_manager.interception_ca import interception_root_ca
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
SHARED_PROVIDERS_IMAGE = "platform-sessions-providers"
REGISTRY = "git.allegedly.works/ducktape-ci"
PROVIDERS = tuple(str(item["id"]) for item in config.INTEGRATIONS["integrations"])
# Roll the canary first; migrate the other providers after checking its data and service.
PIRAEUS_MIGRATIONS = frozenset({"blueshield"})
BEARER_FILES = ("owner", "aiquota", "quest", "thrive-scraper", "thrive-read", "chatgpt-read", "blueshield-read")
_GW_IMAGE = f"{REGISTRY}/{GATEWAY}:unset"
_GATEWAY_DNS = f"{GATEWAY}.{NAMESPACE}.svc"
_TRUST = "/var/run/provider-trust/ca.crt"
_TOKEN_DIR = "/var/run/provider-credentials"
_RECORD_CA_SECRET = "platform-sessions-recorder-ca"
_RECORD_BUNDLE = "platform-sessions-recorder-trust"
_RECORD_CA_DIR = "/var/run/session-recorder-ca"
_RECORD_TRUST_DIR = "/var/run/session-recorder-trust"
_RECORD_PORT = 18080


def _provider_name(instance: str) -> str:
    return f"platform-sessions-{instance}"


def _provider_dns(instance: str) -> str:
    return f"{_provider_name(instance)}.{NAMESPACE}.svc"


def _provider_image(instance: str) -> str:
    if instance in {"openai", "anthropic", "thrive"}:
        return SHARED_PROVIDERS_IMAGE
    return _provider_name(instance)


PROVIDER_IMAGES = tuple(sorted({_provider_image(instance) for instance in PROVIDERS}))


# A separate ConfigMap prevents preparing the new deployment from rolling the live service.
CONFIG = ConfigMapArgs(
    name="platform-sessions-remote-config",
    namespace=NAMESPACE,
    data={
        "gateway-integrations.json": json.dumps(
            {"integrations": [{"id": item["id"], "type": item["type"]} for item in config.INTEGRATIONS["integrations"]]}
        ),
        "bearers.json": json.dumps(config.BEARERS),
        **{
            f"{item['id']}-integration.json": json.dumps({"integrations": [item]})
            for item in config.INTEGRATIONS["integrations"]
        },
        **{
            f"{instance}-routes.json": json.dumps(
                [
                    {"method": route["method"], "path": route["path"]}
                    for bearer in config.BEARERS.values()
                    for route in bearer.get("allow", [])
                    if route["instance"] == instance
                ]
            )
            for instance in PROVIDERS
        },
        "connections.json": json.dumps(
            {
                instance: {
                    "origin": f"https://{_provider_dns(instance)}:8443",
                    "control_target": f"{_provider_dns(instance)}:8444",
                    "control_token_file": f"{_TOKEN_DIR}/{instance}/control",
                    "owner_token_file": f"{_TOKEN_DIR}/{instance}/owner",
                    "route_token_file": f"{_TOKEN_DIR}/{instance}/route",
                    "control_ca_file": _TRUST,
                    "http_ca_file": _TRUST,
                }
                for instance in PROVIDERS
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
    init_containers: list[k8s.Container] | None = None,
) -> None:
    deployment = k8s.KubeDeployment(
        chart,
        f"{name}-deployment",
        metadata=k8s.ObjectMeta(name=name, namespace=NAMESPACE),
        spec=k8s.DeploymentSpec(
            replicas=1,
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
                    init_containers=init_containers,
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


def _add_provider(chart: Chart, instance: str) -> None:
    name = _provider_name(instance)
    for role in ("control", "owner", "route"):
        mint_bearer_secret(
            chart,
            f"{name}-{role}",
            name=f"{name}-{role}",
            namespace=NAMESPACE,
            key="token",
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
            deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
            immutable=True,
        )

    Certificate(
        chart,
        f"{name}-certificate",
        metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
        secret_name=f"{name}-tls",
        issuer_ref=cluster_ca.INTERNAL_ISSUER,
        dns_names=[_provider_dns(instance)],
    )

    k8s.KubePersistentVolumeClaim(
        chart,
        f"{name}-claim",
        metadata=k8s.ObjectMeta(
            name=name, namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"}
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="seaweedfs-ovh-ssd",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("1Gi")}),
        ),
    )

    if instance in PIRAEUS_MIGRATIONS:
        k8s.KubePersistentVolumeClaim(
            chart,
            f"{name}-piraeus",
            metadata=k8s.ObjectMeta(
                name=f"{name}-piraeus",
                namespace=NAMESPACE,
                annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"},
            ),
            spec=k8s.PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                storage_class_name="piraeus-ssd-2-autodiskful",
                resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("1Gi")}),
            ),
        )

    # Keep the capture PVC even when recording is disabled; it contains user data.
    k8s.KubePersistentVolumeClaim(
        chart,
        f"{name}-captures",
        metadata=k8s.ObjectMeta(
            name=f"{name}-captures", namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"}
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="piraeus-hdd-local",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("20Gi")}),
        ),
    )


def _image_policy(chart: Chart, name: str) -> None:
    # Provider images are built by gaffer-private and use main-* tags.
    repository = ImageRepository(
        chart,
        f"{name}-image-repository",
        metadata=ApiObjectMetadata(name=name, namespace="flux-system"),
        image=f"{REGISTRY}/{name}",
        interval="5m",
        secret_ref=ImageRepositorySpecSecretRef(name="forgejo-images-creds"),
    )
    ImagePolicy(
        chart,
        f"{name}-image-policy",
        metadata=ApiObjectMetadata(name=name, namespace="flux-system"),
        image_repository_ref=ImagePolicySpecImageRepositoryRef(name=repository.name),
        filter_tags=ImagePolicySpecFilterTags(pattern=r"^main-\d{14}-[0-9a-f]{7}$"),
        policy=ImagePolicySpecPolicy(
            alphabetical=ImagePolicySpecPolicyAlphabetical(order=ImagePolicySpecPolicyAlphabeticalOrder.ASC)
        ),
    )


def _provider_workload(chart: Chart, instance: str, *, record_upstream_traffic: bool) -> None:
    name = _provider_name(instance)
    migrating = instance in PIRAEUS_MIGRATIONS
    provider_credentials = k8s.Volume(
        name="provider-credentials",
        projected=k8s.ProjectedVolumeSource(
            default_mode=0o440,
            sources=[_secret_projection(f"{name}-{role}", role) for role in ("control", "owner", "route")],
        ),
    )
    config_volume = k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=CONFIG.name))
    tmp = k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource())
    provider_health = k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", scheme="HTTPS", port=k8s.IntOrString.from_string("https"))
    )
    recorder_env = (
        [
            k8s.EnvVar(name="HTTP_PROXY", value=f"http://127.0.0.1:{_RECORD_PORT}"),
            k8s.EnvVar(name="HTTPS_PROXY", value=f"http://127.0.0.1:{_RECORD_PORT}"),
            k8s.EnvVar(name="NO_PROXY", value="localhost,127.0.0.1,.svc,.svc.cluster.local"),
            k8s.EnvVar(name="SSL_CERT_FILE", value=f"{_RECORD_TRUST_DIR}/ca-certificates.crt"),
            k8s.EnvVar(name="PLATFORM_SESSIONS_PROXY_ENABLED", value="true"),
            k8s.EnvVar(name="PLATFORM_SESSIONS_PROXY_CA_FILE", value=f"{_RECORD_CA_DIR}/tls.crt"),
        ]
        if record_upstream_traffic
        else []
    )
    recorder_mounts = (
        [
            k8s.VolumeMount(name="recorder-ca-public", mount_path=_RECORD_CA_DIR, read_only=True),
            k8s.VolumeMount(name="recorder-trust", mount_path=_RECORD_TRUST_DIR, read_only=True),
        ]
        if record_upstream_traffic
        else []
    )
    recorder_volumes = (
        [
            k8s.Volume(
                name="captures",
                persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=f"{name}-captures"),
            ),
            k8s.Volume(name="recorder-ca", secret=k8s.SecretVolumeSource(secret_name=_RECORD_CA_SECRET)),
            k8s.Volume(
                name="recorder-ca-public",
                secret=k8s.SecretVolumeSource(
                    secret_name=_RECORD_CA_SECRET, items=[k8s.KeyToPath(key="tls.crt", path="tls.crt")]
                ),
            ),
            k8s.Volume(name="recorder-trust", config_map=k8s.ConfigMapVolumeSource(name=_RECORD_BUNDLE)),
            k8s.Volume(name="recorder-config", empty_dir=k8s.EmptyDirVolumeSource()),
        ]
        if record_upstream_traffic
        else []
    )
    recorder_init = (
        [
            k8s.Container(
                name="recorder-ca-init",
                image="busybox:1.38",
                command=["sh", "-c", "umask 077; cat /ca/tls.key /ca/tls.crt > /config/mitmproxy-ca.pem"],
                volume_mounts=[
                    k8s.VolumeMount(name="recorder-ca", mount_path="/ca", read_only=True),
                    k8s.VolumeMount(name="recorder-config", mount_path="/config"),
                ],
                security_context=_container_security(),
                resources=_resources(),
            )
        ]
        if record_upstream_traffic
        else []
    )
    recorder_sidecars = (
        [
            k8s.Container(
                name="upstream-recorder",
                image="mitmproxy/mitmproxy:12.2.3",
                # Start the recorder before providers dispatch their first probe.
                restart_policy="Always",
                command=[
                    "sh",
                    "-c",
                    "umask 077; exec mitmdump -q --listen-host 127.0.0.1 --listen-port 18080 --set confdir=/config -w '+/captures/%Y-%m-%d.mitm'",
                ],
                readiness_probe=k8s.Probe(
                    exec=k8s.ExecAction(
                        command=[
                            "python",
                            "-c",
                            "import socket; socket.create_connection(('127.0.0.1', 18080), timeout=2).close()",
                        ]
                    )
                ),
                startup_probe=k8s.Probe(
                    exec=k8s.ExecAction(
                        command=[
                            "python",
                            "-c",
                            "import socket; socket.create_connection(('127.0.0.1', 18080), timeout=2).close()",
                        ]
                    ),
                    period_seconds=2,
                    failure_threshold=30,
                ),
                volume_mounts=[
                    k8s.VolumeMount(name="recorder-config", mount_path="/config"),
                    k8s.VolumeMount(name="captures", mount_path="/captures"),
                ],
                security_context=_container_security(),
                resources=_resources(),
            )
        ]
        if record_upstream_traffic
        else []
    )
    _deployment(
        chart,
        name,
        k8s.Container(
            name="provider",
            image=f"{REGISTRY}/{_provider_image(instance)}:unset",
            args=[
                "--integration-config",
                f"/etc/platform-sessions/{instance}-integration.json",
                "--instance-id",
                instance,
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
            ]
            + (
                [] if instance == "blueshield" else ["--route-grants", f"/etc/platform-sessions/{instance}-routes.json"]
            ),
            env=[k8s.EnvVar(name="HOME", value="/tmp"), *recorder_env],
            ports=[
                k8s.ContainerPort(name="https", container_port=8443),
                k8s.ContainerPort(name="grpc", container_port=8444),
                k8s.ContainerPort(name="metrics", container_port=9091),
            ]
            + ([k8s.ContainerPort(name="quest-metrics", container_port=9090)] if instance == "quest" else []),
            readiness_probe=provider_health,
            liveness_probe=provider_health,
            resources=_resources(),
            security_context=_container_security(),
            volume_mounts=[
                k8s.VolumeMount(name="config", mount_path="/etc/platform-sessions", read_only=True),
                k8s.VolumeMount(name="provider-credentials", mount_path=_TOKEN_DIR, read_only=True),
                k8s.VolumeMount(name="tls", mount_path="/var/run/provider-tls", read_only=True),
                k8s.VolumeMount(name="data", mount_path="/data", sub_path="state" if migrating else None),
                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                *recorder_mounts,
            ],
        ),
        [
            config_volume,
            provider_credentials,
            k8s.Volume(name="tls", secret=k8s.SecretVolumeSource(secret_name=f"{name}-tls")),
            k8s.Volume(
                name="data",
                persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                    claim_name=f"{name}-piraeus" if migrating else name
                ),
            ),
            tmp,
            *(
                [
                    k8s.Volume(
                        name="old-data",
                        persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=name, read_only=True),
                    ),
                    k8s.Volume(
                        name="migration-script",
                        config_map=k8s.ConfigMapVolumeSource(name="platform-sessions-piraeus-migration"),
                    ),
                ]
                if migrating
                else []
            ),
            *recorder_volumes,
        ],
        node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
        # Recreate waits for the old provider to terminate before this init container
        # copies its read-only state. Remove this temporary source mount after all
        # providers have migrated and their Piraeus state has been verified.
        init_containers=[
            *(
                [
                    k8s.Container(
                        name="migrate-state",
                        image=f"{REGISTRY}/{_provider_image(instance)}:unset",
                        command=["python", "-I", "/migration/migrate_state.py"],
                        volume_mounts=[
                            k8s.VolumeMount(name="old-data", mount_path="/source", read_only=True),
                            k8s.VolumeMount(name="data", mount_path="/destination"),
                            k8s.VolumeMount(name="migration-script", mount_path="/migration", read_only=True),
                        ],
                        security_context=_container_security(),
                        resources=_resources(),
                    )
                ]
                if migrating
                else []
            ),
            *recorder_init,
            *recorder_sidecars,
        ],
    )
    _service(
        chart,
        name,
        [
            k8s.ServicePort(name="https", port=8443, target_port=k8s.IntOrString.from_string("https")),
            k8s.ServicePort(name="grpc", port=8444, target_port=k8s.IntOrString.from_string("grpc")),
        ],
    )

    PodMonitor(
        chart,
        f"{name}-monitor",
        metadata=ApiObjectMetadata(name=f"{name}-provider" if instance == "quest" else name, namespace=NAMESPACE),
        selector=PodMonitorSpecSelector(match_labels={"app": name}),
        pod_metrics_endpoints=[PodMonitorSpecPodMetricsEndpoints(port="metrics", path="/metrics", interval="1m")]
        + (
            [PodMonitorSpecPodMetricsEndpoints(port="quest-metrics", path="/metrics", interval="1m")]
            if instance == "quest"
            else []
        ),
    )


def add_resources(chart: Chart, *, record_upstream_traffic: bool) -> None:
    k8s.KubeConfigMap(
        chart,
        "piraeus-migration",
        metadata=k8s.ObjectMeta(name="platform-sessions-piraeus-migration", namespace=NAMESPACE),
        data={"migrate_state.py": Path(__file__).with_name("migrate_state.py").read_text()},
    )
    if record_upstream_traffic:
        interception_root_ca(
            chart,
            name="platform-sessions-recorder-root-ca",
            namespace=NAMESPACE,
            secret_name=_RECORD_CA_SECRET,
            bundle_name=_RECORD_BUNDLE,
            description="Trust bundle for platform-session upstream recording",
            target_namespaces=(NAMESPACE,),
        )
    for instance in PROVIDERS:
        _add_provider(chart, instance)
        _provider_workload(chart, instance, record_upstream_traffic=record_upstream_traffic)
    for image in PROVIDER_IMAGES:
        _image_policy(chart, image)
    Certificate(
        chart,
        f"{GATEWAY}-certificate",
        metadata=ApiObjectMetadata(name=GATEWAY, namespace=NAMESPACE),
        secret_name=f"{GATEWAY}-tls",
        issuer_ref=cluster_ca.INTERNAL_ISSUER,
        dns_names=[_GATEWAY_DNS],
    )
    provider_credentials = k8s.Volume(
        name="provider-credentials",
        projected=k8s.ProjectedVolumeSource(
            default_mode=0o440,
            sources=[
                _secret_projection(f"{_provider_name(instance)}-{role}", f"{instance}/{role}")
                for instance in PROVIDERS
                for role in ("control", "owner", "route")
            ],
        ),
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
                    sources=[_secret_projection(f"platform-sessions-{role}-bearer", role) for role in BEARER_FILES],
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


def chart(app: App, *, record_upstream_traffic: bool = config.RECORD_UPSTREAM_TRAFFIC) -> Chart:
    result = Chart(app, NAME, disable_resource_name_hashes=True)
    add_resources(result, record_upstream_traffic=record_upstream_traffic)
    return result


def platform_sessions_remote(
    chart: Chart,
    directory: RenderedDirectory,
    legacy: Kustomization,
    cert_manager_environment: Kustomization,
    image_automation: Kustomization,
    piraeus: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        description="Platform Sessions gateway and separate providers.",
        suspend=False,
        # The legacy and remote inventories overlap on the Quest monitor. Keep pruning off
        # through its later rename; restore it only after the stale remote entry is gone.
        prune=False,
        wait=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(legacy, cert_manager_environment, image_automation, piraeus),
    )
