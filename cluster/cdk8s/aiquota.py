"""aiquota's API Deployment (with the `migrate` init container that applies its own
ClickHouse schema), Service, HTTPRoute and ServiceMonitor, the pull-credentials
ExternalSecret, and the narrow per-consumer mirrors of its API bearer.

Hand-written beside the generated output (cluster/docs/cdk8s.md): the bearer itself
(aiquota-api-bearer.sops.yaml), the configMapGenerator inputs config.toml and
schema.sql, and image-pins/kustomization.yaml, which overrides the API container's
`unset` placeholder tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import (
    Capability,
    ConfigMap,
    ContainerPort,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    EnvValue,
    ImagePullPolicy,
    ISecret,
    LabelSelector,
    MemoryResources,
    PodSecurityContextProps,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServicePort,
    Volume,
    VolumeMount,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    NAMESPACE as FLUX_NAMESPACE,
    SOPS_DECRYPTION,
    ConfigMapArgs,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret, forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch
from cluster.cdk8s.probes import http_probe

NAME = "aiquota"
OUTPUT_DIR = "cluster/k8s/aiquota"
NAMESPACE = "cli-proxy-api"  # shared with CLIProxyAPI, whose Kustomization creates it
BEARER_SECRET_NAME = "aiquota-api-bearer"  # the SOPS-managed Secret; every mirror below copies its one key
CONFIG_CONFIG_MAP = ConfigMapArgs(name="aiquota-api-config", namespace=NAMESPACE, files=["config.toml"])
SCHEMA_CONFIG_MAP = ConfigMapArgs(name="aiquota-schema", namespace=NAMESPACE, files=[client.SCHEMA_FILE])

_API_NAME = "aiquota-api"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/aiquota-api"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_HOSTNAME = "aiquota.allegedly.works"
_PORT = 8080
_LABELS = {"app.kubernetes.io/name": NAME}
_CONFIG_DIR = "/etc/aiquota"
_BEARER_KEY = "bearer-token"


@dataclass(frozen=True)
class BearerMirror:
    """A reflector-owned copy of the API bearer that only one consumer namespace receives,
    so that namespace never needs access to the broad cli-proxy-api SecretStore."""

    consumer: str
    namespace: str
    description: str

    @property
    def secret_name(self) -> str:
        return f"{BEARER_SECRET_NAME}-{self.consumer}"


BEARER_MIRRORS = (
    # In the destination namespace only the trusted egress proxy consumes this Secret; the
    # OpenClaw workload receives a non-secret placeholder instead.
    BearerMirror(
        consumer="public-coder",
        namespace="public-coder-agent",
        description="Shared AIQuota API bearer mirrored only to public-coder-agent's trusted egress proxy.",
    ),
    # Same shape as public-coder: only the trusted haku-claude-oauth-proxy consumes it, the
    # haku-runtime sandbox receives a placeholder.
    BearerMirror(
        consumer="haku-claude",
        namespace="haku-egress-proxy",
        description="Shared AIQuota API bearer mirrored only to the haku-claude-oauth-proxy egress proxy.",
    ),
    BearerMirror(
        consumer="haku-console",
        namespace="haku-console",
        description="Shared AIQuota API bearer mirrored only to the Haku Console backend.",
    ),
    # Haku's runtimes outside the egress fence (the Claude Code web home, plain kubectl) read
    # /v1/quotas directly with no per-call approval.
    BearerMirror(
        consumer="haku-sandbox",
        namespace="haku-sandbox",
        description="Shared AIQuota API bearer mirrored only to haku-sandbox for Haku's own quota reads.",
    ),
)


def _secret_env(secret: ISecret, key: str) -> EnvValue:
    return EnvValue.from_secret_value(SecretValue(secret=secret, key=key))


class Aiquota(Construct):
    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=NAMESPACE)
        for mirror in BEARER_MIRRORS:
            self._add_bearer_mirror(mirror)
        deployment = self._add_deployment()
        self._add_service(deployment)
        https_route(
            self,
            "httproute",
            metadata=metadata(_API_NAME, NAMESPACE),
            hostname=_HOSTNAME,
            backend=_API_NAME,
            port=_PORT,
            hsts=False,
            listener=None,
        )
        self._add_service_monitor()

    def _add_bearer_mirror(self, mirror: BearerMirror) -> None:
        ExternalSecret(
            self,
            f"bearer-{mirror.consumer}",
            metadata=metadata(mirror.secret_name, NAMESPACE),
            spec=ExternalSecretSpec(
                refresh_interval="1h",
                secret_store_ref=ExternalSecretSpecSecretStoreRef(
                    kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
                    name="kubernetes-cli-proxy-api-secret-store",
                ),
                target=ExternalSecretSpecTarget(
                    name=mirror.secret_name,
                    creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                    deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
                    template=ExternalSecretSpecTargetTemplate(
                        metadata=ExternalSecretSpecTargetTemplateMetadata(
                            annotations={
                                "description": mirror.description,
                                "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                                "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": mirror.namespace,
                                "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                                "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": mirror.namespace,
                            }
                        )
                    ),
                ),
                data=[
                    ExternalSecretSpecData(
                        secret_key=_BEARER_KEY,
                        remote_ref=ExternalSecretSpecDataRemoteRef(key=BEARER_SECRET_NAME, property=_BEARER_KEY),
                    )
                ],
            ),
        )

    def _add_deployment(self) -> Deployment:
        clickhouse_credentials = Secret.from_secret_name(
            self, "clickhouse-credentials-ref", "clickhouse-aiquota-credentials"
        )
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                _API_NAME,
                NAMESPACE,
                labels=_LABELS,
                annotations={
                    "description": "Claude and Codex subscription quota API via the CLIProxyAPI integration.",
                    "reloader.stakater.com/auto": "true",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=1,
            # A Deployment's selector is immutable: keeping the hand-written one lets Flux
            # adopt the live object instead of failing the apply.
            select=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            automount_service_account_token=False,
            enable_service_links=False,
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
            # Applies schema.sql (idempotent CREATE/ALTER ... IF NOT EXISTS) before the API
            # server starts. An init container's pod template is mutable across rollouts
            # (unlike a bare Job's), so a schema change ships in the same PR as the code that
            # needs it -- no separate Job/version to bump. Kubernetes retries a failing init
            # container.
            init_containers=[
                client.queries_file_container(
                    self,
                    "migrate",
                    schema=ConfigMap.from_config_map_name(self, "schema-ref", SCHEMA_CONFIG_MAP.name),
                    credentials=clickhouse_credentials,
                )
            ],
        )
        deployment.select(LabelSelector.of(labels=_LABELS))
        ApiObject.of(deployment).add_json_patch(runtime_default_seccomp_patch())

        bearer = Secret.from_secret_name(self, "bearer-ref", BEARER_SECRET_NAME)
        cli_proxy_api = Secret.from_secret_name(self, "cli-proxy-api-management-ref", "cli-proxy-api-management")
        oidc = Secret.from_secret_name(self, "oidc-ref", "aiquota-oidc")
        deployment.add_container(
            name=_API_NAME,
            image=f"{_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            ports=[ContainerPort(name="http", number=_PORT, protocol=Protocol.TCP)],
            env_variables={
                "AIQUOTA_API_BEARER_TOKEN": _secret_env(bearer, _BEARER_KEY),
                "AIQUOTA_CLIPROXY_API_KEY": _secret_env(cli_proxy_api, "management-password"),
                "AIQUOTA_CLICKHOUSE_URL": EnvValue.from_value(f"http://{client.HOST}:{client.HTTP_PORT}"),
                "AIQUOTA_CLICKHOUSE_DATABASE": EnvValue.from_value("aiquota"),
                "AIQUOTA_CLICKHOUSE_USERNAME": _secret_env(clickhouse_credentials, "username"),
                "AIQUOTA_CLICKHOUSE_PASSWORD": _secret_env(clickhouse_credentials, "password"),
                "AIQUOTA_POLL_INTERVAL_SECONDS": EnvValue.from_value("300"),
                # History endpoints restate the same months on every call; hourly is frequent
                # enough to watch the current day accrue.
                "AIQUOTA_HISTORY_INTERVAL_SECONDS": EnvValue.from_value("3600"),
                # App-owned Authentik authorization-code flow for the browser UI and `/api/v1/*`;
                # the bearer-only `/v1/*` surface remains for clients.
                "AIQUOTA_OAUTH_ISSUER": EnvValue.from_value("https://auth.allegedly.works/application/o/aiquota/"),
                "AIQUOTA_OAUTH_CLIENT_ID": _secret_env(oidc, "client_id"),
                "AIQUOTA_OAUTH_CLIENT_SECRET": _secret_env(oidc, "client_secret"),
                "AIQUOTA_OAUTH_SESSION_SECRET": _secret_env(oidc, "session_secret"),
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(25), limit=Cpu.millis(250)),
                memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
            ),
            readiness=http_probe("/readyz", port=_PORT, initial_delay_seconds=5, failure_threshold=12),
            liveness=http_probe("/healthz", port=_PORT, initial_delay_seconds=10, period_seconds=20),
            security_context=ContainerSecurityContextProps(
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                read_only_root_filesystem=False,
            ),
            volume_mounts=[
                VolumeMount(
                    path=_CONFIG_DIR,
                    volume=Volume.from_config_map(
                        self,
                        "config-volume",
                        ConfigMap.from_config_map_name(self, "config-ref", CONFIG_CONFIG_MAP.name),
                        name="config",
                    ),
                    read_only=True,
                )
            ],
        )
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(
                _API_NAME,
                NAMESPACE,
                labels=_LABELS,
                annotations={
                    "description": (
                        "Internal bearer-authenticated API for normalized and raw Claude and Codex "
                        "subscription quota responses via CLIProxyAPI."
                    )
                },
            ),
            selector=deployment,
            ports=[ServicePort(name="http", port=_PORT, target_port=_PORT, protocol=Protocol.TCP)],
        )

    def _add_service_monitor(self) -> None:
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=metadata(NAME, NAMESPACE),
            spec=ServiceMonitorSpec(
                selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
                endpoints=[ServiceMonitorSpecEndpoints(port="http", path="/metrics", scrape_timeout="15s")],
            ),
        )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Aiquota(chart, NAME)
    return chart


def aiquota(
    flux_chart: Chart,
    root: Path,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    cli_proxy_api: Kustomization,
    external_secrets_operator: Kustomization,
    clickhouse_schema: Kustomization,
    agent_machine_access_tf: Kustomization,
    reflector: Kustomization,
) -> Kustomization:
    name = NAME
    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    rendered_chart = chart(app)
    add_fleet_rules(rendered_chart)
    app.synth()

    kustomization = flux_kustomization(
        flux_chart,
        name,
        description="aiquota API with Claude and Codex quota through the CLIProxyAPI integration.",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace=FLUX_NAMESPACE
            ),
            path=f"./{OUTPUT_DIR}",
            prune=True,
            wait=True,
            # aiquota-api-bearer.sops.yaml (hand-written, listed below) is SOPS-encrypted.
            decryption=SOPS_DECRYPTION,
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                forgejo_images,
                # Provides the shared namespace and the CLIProxyAPI management Secret.
                cli_proxy_api,
                # Materializes the narrow mirrored copies of the API bearer for its
                # consumers; the source Secret stays SOPS-managed here.
                external_secrets_operator,
                # Creates the aiquota database the migrate init container populates.
                clickhouse_schema,
                # Mints the aiquota-oidc Authentik OAuth2 client credentials Secret.
                agent_machine_access_tf,
                # Reflects clickhouse-aiquota-credentials from the clickhouse namespace.
                reflector,
            ),
        ),
    )
    write_yaml(
        out_dir / "kustomization.yaml",
        # aiquota-api-bearer.sops.yaml, config.toml and schema.sql stay hand-written; the
        # generator entries render the latter two into the ConfigMaps the Deployment mounts.
        # See cluster/docs/cdk8s.md.
        kustomize_kustomization(
            resources=[f"{name}.k8s.yaml", f"{BEARER_SECRET_NAME}.sops.yaml"],
            components=["./image-pins"],
            config_map_generator=[CONFIG_CONFIG_MAP, SCHEMA_CONFIG_MAP],
        ),
    )
    return kustomization
