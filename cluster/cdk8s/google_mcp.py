"""google-mcp: a standalone Gmail/Calendar MCP backend for agentplane-staging.

Reuses haku-console's Gmail/Calendar tool code (`x/google_mcp_server`, `haku/console/tools`)
against a *separate* Google credential from haku-console's own per-Operator connections: a
write-scoped Airlock provider (`cluster/k8s/agents/airlock/config.yaml`) whose access token is
ESO-mirrored into *this namespace only* -- never into claude-sandbox, haku-sandbox, or
agentplane-staging directly. The agent reaches Gmail/Calendar only through agentplane's
approval-gated MCP tool calls; it never holds the raw Google token. See
`plans/personal_agents/personal_data_agent.md` and `docs/personal_agents/verdicts.md` for why
that boundary matters.

Everything google-mcp needs is generated into one Kustomization, mirroring ha_mcp.py's shape:
its Namespace, ConfigMap-free Deployment/Service/CiliumNetworkPolicy, and its own ESO-minted
caller-facing bearer (same pattern as ssh_mcp/backend.py's `_bearer_credentials` -- ducktape
mints this value itself, so there is no ciphertext to keep in sync with the cluster's age
recipients). The Google write-token Secret is not minted here: it arrives from Airlock's own
Kustomization via a `ClusterExternalSecret` scoped to this namespace only, so this chart only
ever *references* it by name.

The image tag is a deliberate placeholder ("unset") -- `image-pins/kustomization.yaml`
(hand-written) overrides it at `kustomize build` time via Flux's image-automation marker
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import (
    Capability,
    ContainerPort,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    LabelSelector,
    MemoryResources,
    Namespace,
    PodSecurityContextProps,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServicePort,
    Volume,
)
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import Kustomization, KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import cilium
from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import flux_kustomization, flux_kustomization_depends_on_many, kustomize_kustomization
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret, forgejo_images_creds_secret_ref
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe

_NAME = "google-mcp"
OUTPUT_DIR = f"cluster/k8s/{_NAME}"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/google-mcp"
_PLACEHOLDER_TAG = "unset"
_HTTP_PORT = 8080
GMAIL_MCP_URL = f"http://{_NAME}.{_NAME}.svc.cluster.local:{_HTTP_PORT}/gmail/mcp"
CALENDAR_MCP_URL = f"http://{_NAME}.{_NAME}.svc.cluster.local:{_HTTP_PORT}/calendar/mcp"
BEARER_SECRET_NAME = "google-mcp-bearer"
BEARER_SECRET_KEY = "bearer-token"
# Populated by a ClusterExternalSecret in Airlock's own Kustomization
# (cluster/k8s/agents/airlock/), scoped to this namespace only -- see the module docstring.
GOOGLE_TOKEN_SECRET_NAME = "google-write-access-token"
GOOGLE_TOKEN_SECRET_KEY = "access_token"
_GOOGLE_TOKEN_DIR = "/run/secrets/google-write-token"
_LABELS = {"app.kubernetes.io/name": _NAME}


def _bearer_credentials(scope: Construct) -> None:
    """Mint this pod's caller-facing bearer here, in its own namespace.

    agentplane-staging reads a copy through the `kubernetes-google-mcp-secret-store`
    ClusterSecretStore (cluster/k8s/external-secrets/config/google-mcp-secret-store.yaml) --
    ESO's own cross-namespace read, the same mechanism Airlock's tokens and the Tana PAT
    already use for agentplane-staging, not Stakater Reflector.
    """
    Password(
        scope,
        "bearer-password-generator",
        metadata=metadata(BEARER_SECRET_NAME, _NAME),
        spec=PasswordSpec(length=48, digits=12, symbols=0, no_upper=False, allow_repeat=True),
    )
    ExternalSecret(
        scope,
        "bearer-external-secret",
        metadata=metadata(BEARER_SECRET_NAME, _NAME),
        spec=ExternalSecretSpec(
            refresh_policy=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
            target=ExternalSecretSpecTarget(
                name=BEARER_SECRET_NAME,
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                template=ExternalSecretSpecTargetTemplate(type="Opaque", data={BEARER_SECRET_KEY: "{{ .password }}"}),
            ),
            data_from=[
                ExternalSecretSpecDataFrom(
                    source_ref=ExternalSecretSpecDataFromSourceRef(
                        generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                            api_version="generators.external-secrets.io/v1alpha1",
                            kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                            name=BEARER_SECRET_NAME,
                        )
                    )
                )
            ],
        ),
    )


class GoogleMcpApp(Construct):
    """The Deployment, Service, and Cilium ingress/egress policy."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        _bearer_credentials(self)
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=_NAME)
        deployment = self._add_deployment()
        self._add_service(deployment)
        self._add_network_policy()

    def _add_deployment(self) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(_NAME, _NAME, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=1,
            strategy=DeploymentStrategy.recreate(),
            select=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            automount_service_account_token=False,
            enable_service_links=False,
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
        )
        # The Deployment selector is immutable; retain its existing labels for Flux adoption.
        deployment.select(LabelSelector.of(labels=_LABELS))
        apply_pod_spec_patches(deployment)
        bearer = Secret.from_secret_name(self, "bearer-secret-ref", BEARER_SECRET_NAME)
        deployment.add_container(
            name="server",
            image=f"{_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            env_variables={
                "GOOGLE_MCP_TOKEN_FILE": EnvValue.from_value(f"{_GOOGLE_TOKEN_DIR}/{GOOGLE_TOKEN_SECRET_KEY}"),
                "GOOGLE_MCP_BEARER_TOKEN": EnvValue.from_secret_value(
                    SecretValue(secret=bearer, key=BEARER_SECRET_KEY)
                ),
                "GOOGLE_MCP_HOST": EnvValue.from_value("0.0.0.0"),
                "GOOGLE_MCP_PORT": EnvValue.from_value(str(_HTTP_PORT)),
            },
            ports=[ContainerPort(name="http", number=_HTTP_PORT, protocol=Protocol.TCP)],
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(500)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            readiness=http_probe("/healthz", port=_HTTP_PORT, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=_HTTP_PORT, initial_delay_seconds=15, period_seconds=20),
            security_context=ContainerSecurityContextProps(
                allow_privilege_escalation=False,
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                ensure_non_root=True,
                user=1000,
                group=1000,
                # The aspect_rules_py launcher materializes its venv in the image's runfiles.
                read_only_root_filesystem=False,
            ),
        )
        # Airlock's ClusterExternalSecret fills this Secret from another Kustomization. While it is
        # absent (before the `google-write` consent), the pod waits in ContainerCreating on this
        # mount rather than starting without a token.
        google_token_secret = Secret.from_secret_name(self, "google-token-secret-ref", GOOGLE_TOKEN_SECRET_NAME)
        google_token_volume = Volume.from_secret(self, "google-token-volume", google_token_secret)
        deployment.containers[0].mount(_GOOGLE_TOKEN_DIR, google_token_volume, read_only=True)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(_NAME, _NAME),
            selector=deployment,
            ports=[ServicePort(name="http", port=_HTTP_PORT, target_port=_HTTP_PORT, protocol=Protocol.TCP)],
        )

    def _add_network_policy(self) -> None:
        cilium.network_policy(
            self,
            "network-policy",
            metadata=metadata(_NAME, _NAME),
            selector=_LABELS,
            ingress=[
                cilium.ingress_from(
                    cilium.endpoint_labels("agentplane-staging", "agentplane-actions"), ports=[_HTTP_PORT]
                )
            ],
            egress=[cilium.dns_egress(), cilium.egress_to_fqdns("gmail.googleapis.com", "www.googleapis.com")],
        )


class GoogleMcp(Construct):
    """The whole google-mcp Kustomization: its Namespace and the app."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        Namespace(
            self,
            "namespace",
            metadata=ApiObjectMetadata(
                name=_NAME,
                labels={"name": _NAME, "goldilocks.fairwinds.com/enabled": "false"},
                annotations={"description": "Holds the write-scoped Google OAuth token; never mirrored elsewhere."},
            ),
        )
        GoogleMcpApp(self, "app")


def google_mcp(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    root: Path,
    external_secrets_operator: Kustomization,
    forgejo_images: Kustomization,
) -> Kustomization:
    app_dir = root / OUTPUT_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(app_dir))
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    GoogleMcp(chart, _NAME)
    add_fleet_rules(chart)
    app.synth()

    kustomization = flux_kustomization(
        flux_chart,
        _NAME,
        description="Standalone Gmail/Calendar MCP backend for Agentplane staging.",
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=flux_kustomization_depends_on_many(external_secrets_operator, forgejo_images),
        ),
    )
    write_yaml(
        app_dir / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml"], components=["./image-pins"]),
    )
    return kustomization
