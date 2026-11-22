"""Gateway and separate providers for Platform Sessions."""

from __future__ import annotations

import json
from collections.abc import Mapping

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cert_manager_crds.io.cert_manager import Certificate, CertificateSpec
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
from flux_imagerepository_crds.io.fluxcd.toolkit.image import (
    ImageRepository,
    ImageRepositorySpec,
    ImageRepositorySpecSecretRef,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitor,
    PodMonitorSpec,
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecSelector,
)

from cluster.cdk8s import node_scheduling, piraeus_storage, pod_policy, public_web_pki
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.cert_manager.interception_ca import interception_root_ca
from cluster.cdk8s.container_resources import container_resources
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import initial_request_annotations
from cluster.cdk8s.platform_sessions import config
from cluster.cdk8s.providers.flux.image_policy import ImagePolicy
from cluster.cdk8s.service_ref import Port
from cluster.cdk8s.volume_claim import volume_claim
from cluster.cdk8s.vpa_policy import ContainerPolicy

NAMESPACE = "platform-sessions"
NAME = "platform-sessions-remote"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
GATEWAY = "platform-sessions-gateway"
GATEWAY_PORT = Port(name="http", number=8080)
PUBLIC_HOSTNAME = "platform-sessions.allegedly.works"
_PROVIDER_HTTPS = Port(name="https", number=8443)
_PROVIDER_GRPC = Port(name="grpc", number=8444)
_PROVIDER_METRICS = Port(name="metrics", number=9091)
SHARED_PROVIDERS_IMAGE = "platform-sessions-providers"
REGISTRY = "git.allegedly.works/ducktape-ci"
PROVIDERS = tuple(str(item["id"]) for item in config.INTEGRATIONS["integrations"])
BASE_INTEGRATIONS = [item for item in config.INTEGRATIONS["integrations"] if item["id"] != "myquest-canary"]
BASE_PROVIDERS = tuple(str(item["id"]) for item in BASE_INTEGRATIONS)
BEARER_FILES = (
    "owner",
    "aiquota",
    "quest-scheduler",
    "myquest-read",
    "thrive-scraper",
    "thrive-read",
    "chatgpt-read",
    "blueshield-read",
)
_GW_IMAGE = f"{REGISTRY}/{GATEWAY}:unset"
_TRUST = "/var/run/provider-trust/ca.crt"
_TOKEN_DIR = "/var/run/provider-credentials"
_RECORD_CA_SECRET = "platform-sessions-recorder-ca"
_RECORD_BUNDLE = "platform-sessions-recorder-trust"
_RECORD_CA_DIR = "/var/run/session-recorder-ca"
_RECORD_TRUST_DIR = "/var/run/session-recorder-trust"
_RECORD_PORT = 18080
_CPU_REQUEST = "50m"


def _provider_name(instance: str) -> str:
    return f"platform-sessions-{instance}"


def _provider_dns(instance: str) -> str:
    return f"{instance}.{NAMESPACE}.svc"


def _provider_image(instance: str) -> str:
    if instance == "myquest-canary":
        return _provider_name("myquest")
    if instance in {"openai", "anthropic", "thrive"}:
        return SHARED_PROVIDERS_IMAGE
    return _provider_name(instance)


PROVIDER_IMAGES = tuple(sorted({_provider_image(instance) for instance in PROVIDERS}))


def _connections_json(instances: tuple[str, ...]) -> str:
    return json.dumps(
        {
            instance: {
                "origin": f"https://{_provider_dns(instance)}:{_PROVIDER_HTTPS.number}",
                "control_target": f"{_provider_dns(instance)}:{_PROVIDER_GRPC.number}",
                "control_token_file": f"{_TOKEN_DIR}/{instance}/control",
                "owner_token_file": f"{_TOKEN_DIR}/{instance}/owner",
                "route_token_file": f"{_TOKEN_DIR}/{instance}/route",
                "control_ca_file": _TRUST,
                "http_ca_file": _TRUST,
            }
            for instance in instances
        }
    )


# A separate ConfigMap prevents preparing the new deployment from rolling the live service.
CONFIG = ConfigMapArgs(
    name="remote-config",
    namespace=NAMESPACE,
    data={
        "gateway-integrations.json": json.dumps(
            {"integrations": [{"id": item["id"], "type": item["type"]} for item in BASE_INTEGRATIONS]}
        ),
        "bearers.json": json.dumps(config.BEARERS),
        **{f"{item['id']}-integration.json": json.dumps({"integrations": [item]}) for item in BASE_INTEGRATIONS},
        **{
            f"{instance}-routes.json": json.dumps(
                [
                    {"method": route["method"], "path": route["path"]}
                    for bearer in config.BEARERS.values()
                    for route in bearer.get("allow", [])
                    if route["instance"] == instance
                ]
            )
            for instance in BASE_PROVIDERS
        },
        "connections.json": _connections_json(BASE_PROVIDERS),
    },
)


# The gateway and canary use this ConfigMap. The original ConfigMap stays
# byte-identical, so its content-hashed name does not roll existing providers.
def canary_config(record_upstream_traffic: bool) -> ConfigMapArgs:
    return ConfigMapArgs(
        name="remote-canary-config",
        namespace=NAMESPACE,
        data={
            "gateway-integrations.json": json.dumps(
                {
                    "integrations": [
                        {"id": item["id"], "type": item["type"]} for item in config.INTEGRATIONS["integrations"]
                    ]
                }
            ),
            "connections.json": _connections_json(PROVIDERS),
            "myquest-canary-integration.json": json.dumps(
                {"integrations": [config.myquest_canary_integration(record_upstream_traffic)]}
            ),
        },
    )


CANARY_CONFIG = canary_config(config.MYQUEST_CANARY_RECORD_UPSTREAM_TRAFFIC)


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


def _resources(*, memory_request: str = "128Mi", memory_limit: str = "1Gi") -> k8s.ResourceRequirements:
    return container_resources(cpu=_CPU_REQUEST, memory=memory_request, cpu_limit="1", memory_limit=memory_limit)


def _deployment(
    chart: Chart,
    name: str,
    container: k8s.Container,
    volumes: list[k8s.Volume],
    *,
    selector_name: str,
    node_selector: Mapping[str, str] | None = None,
    init_containers: list[k8s.Container] | None = None,
    vpa_policy: ContainerPolicy | None = None,
) -> None:
    deployment = k8s.KubeDeployment(
        chart,
        f"{name}-deployment",
        metadata=k8s.ObjectMeta(
            name=name,
            namespace=NAMESPACE,
            annotations=initial_request_annotations(vpa_policy) if vpa_policy is not None else None,
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels={"app": selector_name}),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels={"app": selector_name}),
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


def _config_env(name: str, key: str, *, config_map: ConfigMapArgs = CONFIG) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name,
        value_from=k8s.EnvVarSource(config_map_key_ref=k8s.ConfigMapKeySelector(name=config_map.name, key=key)),
    )


def _service(chart: Chart, name: str, ports: list[k8s.ServicePort], *, selector_name: str) -> None:
    k8s.KubeService(
        chart,
        f"{name}-service",
        metadata=k8s.ObjectMeta(name=name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector={"app": selector_name}, ports=ports),
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
        metadata=ApiObjectMetadata(name=instance, namespace=NAMESPACE),
        spec=CertificateSpec(
            secret_name=f"{name}-tls", issuer_ref=cluster_ca.INTERNAL_ISSUER, dns_names=[_provider_dns(instance)]
        ),
    )

    volume_claim(
        chart,
        f"{name}-piraeus",
        name=f"{name}-piraeus",
        namespace=NAMESPACE,
        storage_class=piraeus_storage.SSD_REPLICATED,
        size="1Gi",
        annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"},
    )

    # Keep the capture PVC even when recording is disabled; it contains user data.
    volume_claim(
        chart,
        f"{name}-captures",
        name=f"{name}-captures",
        namespace=NAMESPACE,
        storage_class=piraeus_storage.HDD_LOCAL,
        size="20Gi",
        annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"},
    )


def _image_policy(chart: Chart, name: str) -> None:
    # Provider images are built by gaffer-private and use main-* tags.
    repository = ImageRepository(
        chart,
        f"{name}-image-repository",
        metadata=ApiObjectMetadata(name=name, namespace="flux-system"),
        spec=ImageRepositorySpec(
            image=f"{REGISTRY}/{name}",
            interval="5m",
            secret_ref=ImageRepositorySpecSecretRef(name="forgejo-images-creds"),
        ),
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


def _provider_workload(chart: Chart, instance: str, *, provider_type: str, record_upstream_traffic: bool) -> None:
    name = _provider_name(instance)
    provider_credentials = k8s.Volume(
        name="provider-credentials",
        projected=k8s.ProjectedVolumeSource(
            default_mode=0o440,
            sources=[_secret_projection(f"{name}-{role}", role) for role in ("control", "owner", "route")],
        ),
    )
    config_volume = k8s.Volume(
        name="config",
        config_map=k8s.ConfigMapVolumeSource(name=CANARY_CONFIG.name if instance == "myquest-canary" else CONFIG.name),
    )
    tmp = k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource())
    provider_health_get = k8s.HttpGetAction(
        path="/healthz", scheme="HTTPS", port=k8s.IntOrString.from_string(_PROVIDER_HTTPS.name)
    )
    provider_health = k8s.Probe(http_get=provider_health_get)
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
    recorder_listening = k8s.ExecAction(
        command=[
            "python",
            "-c",
            f"import socket; socket.create_connection(('127.0.0.1', {_RECORD_PORT}), timeout=2).close()",
        ]
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
                    f"umask 077; exec mitmdump -q --listen-host 127.0.0.1 --listen-port {_RECORD_PORT} --set confdir=/config -w '+/captures/%Y-%m-%d.mitm'",
                ],
                readiness_probe=k8s.Probe(exec=recorder_listening),
                startup_probe=k8s.Probe(exec=recorder_listening, period_seconds=2, failure_threshold=30),
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
    # Retain the memory reservations for providers with sampled working-set peaks near those requests.
    # Recorder and other init-container resources are outside these policies.
    vpa_policy: ContainerPolicy | None
    match instance:
        case "anthropic" | "chatgpt" | "openai":
            vpa_policy = ContainerPolicy.cpu_and_memory_requests_only("provider", memory_floor="128Mi")
        case "blueshield":
            vpa_policy = ContainerPolicy.cpu_and_memory_requests_only("provider", memory_floor="416Mi")
        case "quest-scheduler":
            vpa_policy = ContainerPolicy.cpu_and_memory_requests_only("provider", memory_floor="576Mi")
        case "myquest":
            # The browser peaked at 986 MiB and used 158m CPU over five minutes during startup.
            vpa_policy = ContainerPolicy.cpu_and_memory_requests_only("provider", cpu_floor="175m", memory_floor="1Gi")
        case "thrive":
            # Reserve above the 652 MiB sampled peak and retain the existing CPU reservation.
            vpa_policy = ContainerPolicy.cpu_and_memory_requests_only(
                "provider", cpu_floor=_CPU_REQUEST, memory_floor="768Mi"
            )
        case _:
            vpa_policy = None
    _deployment(
        chart,
        instance,
        k8s.Container(
            name="provider",
            image=f"{REGISTRY}/{_provider_image(instance)}:unset",
            args=[
                "--integration-config",
                f"/etc/platform-sessions/{instance}-integration.json",
                "--instance-id",
                instance,
                "--control-address",
                f"0.0.0.0:{_PROVIDER_GRPC.number}",
                "--control-token-file",
                f"{_TOKEN_DIR}/control",
                "--owner-token-file",
                f"{_TOKEN_DIR}/owner",
                "--route-token-file",
                f"{_TOKEN_DIR}/route",
                "--http-port",
                str(_PROVIDER_HTTPS.number),
                "--metrics-port",
                str(_PROVIDER_METRICS.number),
                "--tls-cert",
                "/var/run/provider-tls/tls.crt",
                "--tls-key",
                "/var/run/provider-tls/tls.key",
            ]
            + (
                []
                if instance in {"blueshield", "myquest", "myquest-canary"}
                else ["--route-grants", f"/etc/platform-sessions/{instance}-routes.json"]
            ),
            env=[
                k8s.EnvVar(name="HOME", value="/tmp"),
                *recorder_env,
                *(
                    [k8s.EnvVar(name="MYQUEST_BROWSER_RECORD_HAR", value="true")]
                    if instance == "myquest-canary"
                    else []
                ),
                *(
                    [
                        k8s.EnvVar(name="PLATFORM_SESSIONS_PROXY_ENABLED", value="false"),
                        k8s.EnvVar(name="MYQUEST_BROWSER_LAUNCH_MODE", value="cdp"),
                    ]
                    if instance in {"myquest", "myquest-canary"} and not record_upstream_traffic
                    else []
                ),
            ],
            ports=[
                k8s.ContainerPort(name=port.name, container_port=port.number)
                for port in (_PROVIDER_HTTPS, _PROVIDER_GRPC, _PROVIDER_METRICS)
            ],
            readiness_probe=provider_health,
            liveness_probe=provider_health,
            startup_probe=(
                k8s.Probe(http_get=provider_health_get, period_seconds=5, failure_threshold=12)
                if provider_type == "myquest"
                else None
            ),
            resources=(
                _resources(memory_request="1Gi", memory_limit="2Gi")
                if instance in {"myquest", "myquest-canary"}
                else _resources()
            ),
            security_context=_container_security(),
            volume_mounts=[
                k8s.VolumeMount(name="config", mount_path="/etc/platform-sessions", read_only=True),
                k8s.VolumeMount(name="provider-credentials", mount_path=_TOKEN_DIR, read_only=True),
                k8s.VolumeMount(name="tls", mount_path="/var/run/provider-tls", read_only=True),
                k8s.VolumeMount(name="data", mount_path="/data", sub_path="state"),
                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                *recorder_mounts,
            ],
        ),
        [
            config_volume,
            provider_credentials,
            k8s.Volume(name="tls", secret=k8s.SecretVolumeSource(secret_name=f"{name}-tls")),
            k8s.Volume(
                name="data", persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=f"{name}-piraeus")
            ),
            tmp,
            *recorder_volumes,
        ],
        selector_name=name,
        node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
        init_containers=[*recorder_init, *recorder_sidecars],
        vpa_policy=vpa_policy,
    )
    _service(
        chart,
        instance,
        [
            k8s.ServicePort(name=port.name, port=port.number, target_port=k8s.IntOrString.from_string(port.name))
            for port in (_PROVIDER_HTTPS, _PROVIDER_GRPC)
        ],
        selector_name=name,
    )

    PodMonitor(
        chart,
        f"{name}-monitor",
        metadata=ApiObjectMetadata(name=instance, namespace=NAMESPACE),
        spec=PodMonitorSpec(
            selector=PodMonitorSpecSelector(match_labels={"app": name}),
            pod_metrics_endpoints=[
                PodMonitorSpecPodMetricsEndpoints(port=_PROVIDER_METRICS.name, path="/metrics", interval="1m")
            ],
        ),
    )


def add_resources(chart: Chart, *, record_upstream_traffic: bool) -> None:
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
    for integration in config.INTEGRATIONS["integrations"]:
        instance = str(integration["id"])
        _add_provider(chart, instance)
        if instance == "myquest-canary":
            provider_recording = config.MYQUEST_CANARY_RECORD_UPSTREAM_TRAFFIC
        elif instance == "myquest":
            provider_recording = False
        else:
            provider_recording = record_upstream_traffic
        _provider_workload(
            chart, instance, provider_type=str(integration["type"]), record_upstream_traffic=provider_recording
        )
    for image in PROVIDER_IMAGES:
        _image_policy(chart, image)
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
    gateway_health = k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string(GATEWAY_PORT.name))
    )
    _deployment(
        chart,
        "gateway",
        k8s.Container(
            name="gateway",
            image=_GW_IMAGE,
            env_from=[k8s.EnvFromSource(secret_ref=k8s.SecretEnvSource(name="platform-sessions-oidc"))],
            env=[
                k8s.EnvVar(name="PLATFORM_SESSIONS_PUBLIC_BASE_URL", value=f"https://{PUBLIC_HOSTNAME}"),
                public_web_pki.SSL_CERT_FILE,
                _config_env(
                    "PLATFORM_SESSIONS_INTEGRATION_CONFIG", "gateway-integrations.json", config_map=CANARY_CONFIG
                ),
                _config_env("PLATFORM_SESSIONS_BEARERS", "bearers.json"),
                _config_env("PLATFORM_SESSIONS_PROVIDER_CONNECTIONS", "connections.json", config_map=CANARY_CONFIG),
            ],
            ports=[k8s.ContainerPort(name=GATEWAY_PORT.name, container_port=GATEWAY_PORT.number)],
            readiness_probe=gateway_health,
            liveness_probe=gateway_health,
            resources=_resources(),
            security_context=_container_security(),
            volume_mounts=[
                k8s.VolumeMount(name="provider-credentials", mount_path=_TOKEN_DIR, read_only=True),
                public_web_pki.VOLUME_MOUNT,
                k8s.VolumeMount(name="bearers", mount_path="/var/run/platform-sessions", read_only=True),
                k8s.VolumeMount(name="trust", mount_path="/var/run/provider-trust", read_only=True),
            ],
        ),
        [
            public_web_pki.VOLUME,
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
        ],
        selector_name=GATEWAY,
        vpa_policy=ContainerPolicy.cpu_and_memory_requests_only("gateway"),
    )
    _service(
        chart,
        "gateway",
        [
            k8s.ServicePort(
                name=GATEWAY_PORT.name,
                port=GATEWAY_PORT.number,
                target_port=k8s.IntOrString.from_string(GATEWAY_PORT.name),
            )
        ],
        selector_name=GATEWAY,
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
        wait=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(legacy, cert_manager_environment, image_automation, piraeus),
    )
