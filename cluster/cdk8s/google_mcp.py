"""google-mcp: a Gmail/Calendar MCP backend for agentplane-staging.

Serves the Gmail/Calendar tool code in `haku/console/tools` (`x/google_mcp_server`) against a
write-scoped Airlock provider (`cluster/cdk8s/airlock.py`'s `OAUTH_CONFIG`) whose access token is
ESO-mirrored into *this namespace only* -- never into claude-sandbox, haku-sandbox, or
agentplane-staging directly. The agent reaches Gmail/Calendar only through agentplane's
approval-gated MCP tool calls; it never holds the raw Google token. See
`plans/personal_agents/personal_data_agent.md` and `docs/personal_agents/verdicts.md` for why
that boundary matters.

Everything google-mcp needs is generated into one Kustomization, mirroring ha_mcp.py's shape:
its Namespace, ConfigMap-free Deployment/Service/CiliumNetworkPolicy, and its own ESO-minted
caller-facing bearer (`external_secrets.minted_secret.mint_bearer_secret` -- ducktape mints
this value itself, so there is no ciphertext to keep in sync with the cluster's age
recipients). The Google write-token Secret is not minted here: it arrives from Airlock's own
Kustomization via a `ClusterExternalSecret` scoped to this namespace only, so this chart only
ever *references* it by name.

The image tag is a deliberate placeholder ("unset") -- the hand-written `PINS_DIR` Component,
which the kustomization includes across the roots, overrides it at `kustomize build` time via
Flux's image-automation marker (cluster/cdk8s/AGENTS.md § the `:tag` Setters marker).
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import (
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    LabelSelector,
    MemoryResources,
    PodSecurityContextProps,
    Secret,
    Service,
    Volume,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import cilium, namespaces, pod_policy
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.forgejo_registry.pull_credentials import (
    forgejo_images_creds_external_secret,
    forgejo_images_creds_secret_ref,
)
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.vpa_policy import ContainerPolicy

NAME = "google-mcp"
_CPU_REQUEST_MILLIS = 50
_CPU_REQUEST = f"{_CPU_REQUEST_MILLIS}m"
_MEMORY_LIMIT_MIB = 512
_MEMORY_LIMIT = f"{_MEMORY_LIMIT_MIB}Mi"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/google-mcp-image-pins"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/google-mcp"
_PLACEHOLDER_TAG = "unset"
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAME, labels=(("app.kubernetes.io/name", NAME),)),
)
# The caller-facing bearer this chart mints.
BEARER = SecretRef(namespace=NAME, name="google-mcp-bearer").key("bearer-token")
# Populated by a ClusterExternalSecret in Airlock's own Kustomization
# (cluster/k8s/agents/airlock/), scoped to this namespace only -- see the module docstring.
GOOGLE_TOKEN_SECRET_NAME = "google-write-access-token"
GOOGLE_TOKEN_SECRET_KEY = "access_token"
_GOOGLE_TOKEN_DIR = "/run/secrets/google-write-token"


class GoogleMcpApp(Construct):
    """The Deployment, Service, and Cilium ingress/egress policy."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        # Mint this pod's caller-facing bearer here, in its own namespace.
        #
        # agentplane-staging copies it with ESO through a store that can read this one Secret
        # (cluster/cdk8s/agentplane/staging.py): this namespace also holds the write-scoped Google
        # token, which no store may reach.
        mint_bearer_secret(
            self,
            "bearer-external-secret",
            name=BEARER.secret.name,
            namespace=BEARER.secret.namespace,
            key=BEARER.key,
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        )
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=NAME)
        deployment = self._add_deployment()
        self._add_service(deployment)
        self._add_network_policy()

    def _add_deployment(self) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=NAME,
                namespace=NAME,
                labels=SERVICE.pods.selector,
                annotations=namespaces.initial_request_annotations(
                    # Peak 30-day working set was 196Mi, including startup.
                    ContainerPolicy.cpu_and_memory_requests_only(
                        "server",
                        cpu_floor=_CPU_REQUEST,
                        cpu_ceiling="75m",
                        memory_floor="224Mi",
                        memory_ceiling=_MEMORY_LIMIT,
                    )
                ),
            ),
            pod_metadata=ApiObjectMetadata(labels=SERVICE.pods.selector),
            replicas=1,
            strategy=DeploymentStrategy.recreate(),
            select=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            automount_service_account_token=False,
            enable_service_links=False,
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
        )
        # The Deployment selector is immutable; retain its existing labels for Flux adoption.
        deployment.select(LabelSelector.of(labels=SERVICE.pods.selector))
        deployment.add_container(
            name="server",
            image=f"{_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            env_variables={
                "GOOGLE_MCP_TOKEN_FILE": EnvValue.from_value(f"{_GOOGLE_TOKEN_DIR}/{GOOGLE_TOKEN_SECRET_KEY}"),
                "GOOGLE_MCP_BEARER_TOKEN": BEARER.env_value(self, "bearer-secret-ref"),
                "GOOGLE_MCP_HOST": EnvValue.from_value("0.0.0.0"),
                "GOOGLE_MCP_PORT": EnvValue.from_value(str(SERVICE.pod_port)),
            },
            ports=[SERVICE.port.container_port()],
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(_CPU_REQUEST_MILLIS), limit=Cpu.millis(500)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(_MEMORY_LIMIT_MIB)),
            ),
            readiness=http_probe("/healthz", port=SERVICE.pod_port, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=SERVICE.pod_port, initial_delay_seconds=15, period_seconds=20),
            security_context=ContainerSecurityContextProps(
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
        pod_policy.harden(deployment)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=SERVICE.name, namespace=NAME),
            selector=deployment,
            ports=[SERVICE.port.service_port()],
        )

    def _add_network_policy(self) -> None:
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAME),
            endpoint_selector=SERVICE.pods.selector,
            ingress=[
                IngressRule.from_endpoints(
                    cilium.endpoint_labels("agentplane-staging", "actions"), ports=[SERVICE.pod_port]
                )
            ],
            egress=[
                cilium.dns_egress(resolves=["*"]),
                EgressRule.to_fqdns("gmail.googleapis.com", "www.googleapis.com"),
            ],
        )


class GoogleMcp(Construct):
    """The whole google-mcp Kustomization: its Namespace and the app."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        namespaces.namespace(
            self,
            "namespace",
            name=NAME,
            vpa=Vpa.RECOMMEND,
            labels={"name": NAME},
            annotations={"description": "Holds the write-scoped Google OAuth token; never mirrored elsewhere."},
        )
        GoogleMcpApp(self, "app")


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    GoogleMcp(chart, NAME)
    return chart
