"""aiquota's API Deployment (with the `migrate` init container that applies its own
ClickHouse schema), Service, HTTPRoute and ServiceMonitor, the pull-credentials
ExternalSecret, and the narrow per-consumer mirrors of its API bearer.

Hand-written beside the generated output (cluster/docs/cdk8s.md): the bearer itself
(aiquota-api-bearer.sops.yaml), the configMapGenerator input schema.sql, and
image-pins/kustomization.yaml, which overrides the API container's `unset` placeholder
tag.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import tomli_w
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
    k8s,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from aiquota.api import Settings
from aiquota.config import Config
from cluster.cdk8s import public_coder_proxy
from cluster.cdk8s.agentplane.egress_credentials import STAGING_NAMESPACE
from cluster.cdk8s.cli_proxy_api import cli_proxy_api as cli_proxy_api_app  # aiquota()'s parameter is its Kustomization
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    ConfigMapArgs,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret, forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from util.settings_contract import checked_value, env_name, settings_file

NAME = "aiquota"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/aiquota"
NAMESPACE = cli_proxy_api_app.NAMESPACE  # created by CLIProxyAPI's Kustomization
BEARER_SECRET_NAME = "aiquota-api-bearer"  # the SOPS-managed Secret; every mirror below copies its one key
# The API reads its config at the Settings default; nothing sets AIQUOTA_CONFIG.
_CONFIG_PATH: Path = Settings.model_fields["config_path"].default
_CONFIG_HEADER = textwrap.dedent(
    """\
    # The CLIProxyAPI integration keeps the Claude and Codex OAuth credentials and
    # performs the authenticated usage requests. The legacy Claude setup token
    # remains owned by Haku's existing egress proxy, not by this Deployment.

    """
)
_CONFIG = settings_file(
    Config,
    {
        "cli_proxy_api": {
            "url": (
                f"http://{cli_proxy_api_app.NAME}.{cli_proxy_api_app.NAMESPACE}.svc.cluster.local:"
                f"{cli_proxy_api_app.PORT}/v0/management"
            )
        },
        "claude": {"enabled": True},
        "codex": {"enabled": True},
        "zai": {"enabled": False},
    },
)
CONFIG_CONFIG_MAP = ConfigMapArgs(
    name="aiquota-api-config",
    namespace=NAMESPACE,
    literals=[f"{_CONFIG_PATH.name}={_CONFIG_HEADER}{tomli_w.dumps(_CONFIG)}"],
)
SCHEMA_CONFIG_MAP = ConfigMapArgs(name="aiquota-schema", namespace=NAMESPACE, files=[client.SCHEMA_FILE])

_API_NAME = "aiquota-api"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/aiquota-api"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_HOSTNAME = "aiquota.allegedly.works"
_PORT = 8080
_LABELS = {"app.kubernetes.io/name": NAME}
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

    @property
    def secret_key_selector(self) -> k8s.SecretKeySelector:
        """What a consumer's env reference names."""
        return k8s.SecretKeySelector(name=self.secret_name, key=_BEARER_KEY)


# In the destination namespace only the trusted egress proxy consumes this Secret; the OpenClaw
# workload receives a non-secret placeholder instead.
PUBLIC_CODER_BEARER = BearerMirror(
    consumer="public-coder",
    namespace=public_coder_proxy.NAMESPACE,
    description="Shared AIQuota API bearer mirrored only to public-coder-agent's trusted egress proxy.",
)
# The same for agentplane-staging's egress proxy (agentplane/egress_staging_credentials.py).
AGENTPLANE_STAGING_BEARER = BearerMirror(
    consumer="agentplane-staging",
    namespace=STAGING_NAMESPACE,
    description="Shared AIQuota API bearer mirrored only to agentplane-staging's egress proxy credentials.",
)

BEARER_MIRRORS = (
    PUBLIC_CODER_BEARER,
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
    AGENTPLANE_STAGING_BEARER,
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
            name=mirror.secret_name,
            namespace=NAMESPACE,
            refresh="1h",
            store=SecretStoreRef.cluster("kubernetes-cli-proxy-api-secret-store"),
            data=[remote_data(BEARER_SECRET_NAME, _BEARER_KEY)],
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
                env_name(Settings, "api_bearer_token"): _secret_env(bearer, _BEARER_KEY),
                env_name(Settings, "cli_proxy_api_key"): _secret_env(cli_proxy_api, "management-password"),
                env_name(Settings, "clickhouse_url"): EnvValue.from_value(f"http://{client.HOST}:{client.HTTP_PORT}"),
                env_name(Settings, "clickhouse_database"): EnvValue.from_value("aiquota"),
                env_name(Settings, "clickhouse_username"): _secret_env(clickhouse_credentials, "username"),
                env_name(Settings, "clickhouse_password"): _secret_env(clickhouse_credentials, "password"),
                env_name(Settings, "poll_interval_seconds"): EnvValue.from_value(
                    str(checked_value(Settings, "poll_interval_seconds", 300))
                ),
                # History endpoints restate the same months on every call; hourly is frequent
                # enough to watch the current day accrue.
                env_name(Settings, "history_interval_seconds"): EnvValue.from_value(
                    str(checked_value(Settings, "history_interval_seconds", 3600))
                ),
                # App-owned Authentik authorization-code flow for the browser UI and `/api/v1/*`;
                # the bearer-only `/v1/*` surface remains for clients.
                env_name(Settings, "oauth_issuer"): EnvValue.from_value(
                    "https://auth.allegedly.works/application/o/aiquota/"
                ),
                env_name(Settings, "oauth_client_id"): _secret_env(oidc, "client_id"),
                env_name(Settings, "oauth_client_secret"): _secret_env(oidc, "client_secret"),
                env_name(Settings, "oauth_session_secret"): _secret_env(oidc, "session_secret"),
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
                    path=str(_CONFIG_PATH.parent),
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
            selector=_LABELS,
            endpoints=[Endpoint.plain(port="http", scrape_timeout="15s")],
        )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Aiquota(chart, NAME)
    return chart


def aiquota(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
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
        artifact,
        description="aiquota API with Claude and Codex quota through the CLIProxyAPI integration.",
        timeout="5m",
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
    )
    write_yaml(
        out_dir / "kustomization.yaml",
        # aiquota-api-bearer.sops.yaml and schema.sql stay hand-written (cluster/docs/cdk8s.md);
        # the generator entries render schema.sql and the config into the ConfigMaps the
        # Deployment mounts.
        kustomize_kustomization(
            resources=[f"{name}.k8s.yaml", f"{BEARER_SECRET_NAME}.sops.yaml"],
            components=["./image-pins"],
            config_map_generator=[CONFIG_CONFIG_MAP, SCHEMA_CONFIG_MAP],
        ),
    )
    return kustomization
