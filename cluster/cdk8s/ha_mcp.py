"""Everything ha-mcp needs, generated into one Kustomization: its Namespace, the ESO copy of
the Home Assistant token the Home Assistant provisioner keeps valid (cluster/cdk8s/home_assistant),
and the ConfigMap/Deployment/Service/CiliumNetworkPolicy/ServiceMonitor for the MCP server itself.
cdk8s generates all of it, so there's no real need to keep the Namespace/credentials/app split
into separate directories the way hand-written manifests once did -- see cluster/docs/cdk8s.md.

The facade container's image tag is a deliberate placeholder ("unset") -- the hand-written
`PINS_DIR` Component, which the directory includes across the roots, overrides it at
`kustomize build` time via Flux's image-automation marker. The ha-mcp container's own
image is pinned by digest directly and isn't Flux-managed.

The facade's static bearer token is minted by ESO's Password generator
(`external_secrets.minted_secret.mint_bearer_secret`), not hand-written SOPS -- ducktape
mints this value itself, so there is no ciphertext to keep in sync with the cluster's age
recipients.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import (
    Capability,
    ConfigMap,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    EnvFrom,
    ImagePullPolicy,
    MemoryResources,
    Service,
    ServiceAccount,
    Volume,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import cilium, namespaces, pod_policy
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_external_secret, forgejo_images_creds_secret_ref
from cluster.cdk8s.home_assistant import app as home_assistant  # a bare `app.SERVICE` would not say whose
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_NAME = "ha-mcp"
_NAMESPACE = "ha-mcp"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/ha-mcp/app"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/agents/ha-mcp/app-image-pins"
# The ESO copy of the token the Home Assistant provisioner keeps valid.
_HOME_ASSISTANT_TOKEN = SecretRef(namespace=_NAMESPACE, name="ha-mcp-home-assistant-token").key("token")
# The facade's static client bearer; agentplane-staging copies it.
BEARER = SecretRef(namespace=_NAMESPACE, name="ha-mcp-bearer").key("bearer-token")
_PLACEHOLDER_TAG = "unset"

_APP_NAME = "ha-mcp"
_APP_FACADE_IMAGE_NAME = "git.allegedly.works/ducktape-ci/mcp-oauth-facade"
_APP_CONFIG_MAP_NAME = "config"
# The ha-mcp server, which the facade reaches over the Pod's loopback.
_UPSTREAM = Port(name="upstream", number=8086)
# The facade clients call, and its metrics port on the same Service and Pods.
FACADE = ServiceRef(
    name=_APP_NAME,
    port=Port(name="http", number=8765),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _APP_NAME),)),
)
_METRICS = ServiceRef(name=FACADE.name, port=Port(name="metrics", number=9090), pods=FACADE.pods)
_APP_DATA_DIR = "/data"


def _home_assistant_token(scope: Construct) -> None:
    """ESO copy of the token the Home Assistant provisioner keeps valid in its own namespace, read
    through a store that can get that one Secret."""
    reader = ServiceAccount(
        scope,
        "home-assistant-token-reader",
        metadata=ApiObjectMetadata(name="home-assistant-token-reader", namespace=_NAMESPACE),
    )
    ExternalSecret(
        scope,
        "home-assistant-token",
        metadata=ApiObjectMetadata(
            name=_HOME_ASSISTANT_TOKEN.secret.name, namespace=_HOME_ASSISTANT_TOKEN.secret.namespace
        ),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(
            single_secret_store(
                scope,
                "ha-mcp-home-assistant-token",
                reader=reader,
                source_namespace=home_assistant.HA_MCP_TOKEN.secret_namespace,
                source_secret=home_assistant.HA_MCP_TOKEN.secret_name,
                consumer_namespace=_NAMESPACE,
            )
        ),
        data=[remote_data(home_assistant.HA_MCP_TOKEN.secret_name, "token", secret_key=_HOME_ASSISTANT_TOKEN.key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )


class HaMcpApp(Construct):
    """The ha-mcp Deployment/Service/ConfigMap/NetworkPolicy/ServiceMonitor and its pull credentials."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=_NAMESPACE)
        # The facade's static bearer token: ducktape mints it itself, so ESO's Password
        # generator creates it directly -- no hand-written SOPS ciphertext to keep in sync
        # with cluster recipients.
        #
        # agentplane-staging copies it with ESO through a store that can read this one Secret
        # (cluster/cdk8s/agentplane/staging.py): this namespace also holds the Home Assistant admin
        # token, which no store may reach.
        mint_bearer_secret(
            self,
            "bearer-external-secret",
            name=BEARER.secret.name,
            namespace=BEARER.secret.namespace,
            key=BEARER.key,
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        )
        config_map = self._add_config_map()
        deployment = self._add_deployment(config_map)
        self._add_service(deployment)
        self._add_network_policy()
        self._add_service_monitor()

    def _add_config_map(self) -> ConfigMap:
        return ConfigMap(
            self,
            "config",
            metadata=ApiObjectMetadata(name=_APP_CONFIG_MAP_NAME, namespace=_NAMESPACE),
            data={
                "HOMEASSISTANT_URL": home_assistant.SERVICE.url,
                "MCP_HOST": "0.0.0.0",
                "MCP_PORT": str(_UPSTREAM.number),
                "MCP_SECRET_PATH": "/mcp",
                "MCP_HEALTHZ": "true",
                "MCP_SERVER_NAME": "Home Assistant",
                "ENVIRONMENT": "production",
                "LOG_LEVEL": "INFO",
                "BACKUP_HINT": "normal",
                "HA_MCP_CONFIG_DIR": _APP_DATA_DIR,
                "HAMCP_BACKUP_DIR": f"{_APP_DATA_DIR}/backups",
                "ENABLE_TOOL_SEARCH": "false",
                "READ_ONLY_MODE": "false",
                "ENABLE_BETA_FEATURES": "false",
                "ENABLE_CODE_MODE": "false",
                "HAMCP_ENABLE_FILESYSTEM_TOOLS": "false",
                "ENABLE_YAML_CONFIG_EDITING": "false",
                "HAMCP_ENABLE_DEV_MODE": "false",
                "ENABLE_TOOL_SECURITY_POLICIES": "false",
                "MCP_FACADE_FACADE_NAME": "Home Assistant MCP Facade",
                "MCP_FACADE_UPSTREAM__KIND": "http",
                "MCP_FACADE_UPSTREAM__URL": f"http://localhost:{_UPSTREAM.number}/mcp",
                # Client auth is a cluster-internal static bearer (MCP_FACADE_CLIENT_AUTH__STATIC_BEARER,
                # injected from the ha-mcp-bearer Secret below), not the public Authentik OAuth gate.
                # FacadeSettings requires exactly one of `auth` / `client_auth`, so the MCP_FACADE_AUTH__*
                # keys are absent by design. With no OAuth there is no client registration or token state
                # to keep, so the facade needs no persistence backend and the Valkey is gone with it.
            },
        )

    def _add_deployment(self, config_map: ConfigMap) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=_APP_NAME,
                namespace=_NAMESPACE,
                labels=FACADE.pods.selector,
                annotations={
                    "description": (
                        "Writable Home Assistant MCP server behind the shared MCP facade, cluster-internal "
                        "and gated by a static bearer that only agentplane-staging's Action Service holds. "
                        "The upstream HA token remains server-side, and the Action Service applies its own "
                        "per-call approval policy."
                    )
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=FACADE.pods.selector),
            replicas=1,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            automount_service_account_token=False,
        )

        tmp_volume = Volume.from_empty_dir(self, "tmp-volume", "tmp")
        data_volume = Volume.from_empty_dir(self, "data-volume", "data")

        deployment.add_container(
            name="ha-mcp",
            image="ghcr.io/homeassistant-ai/ha-mcp:8.4.3@sha256:d5cea47a0115e5d161c2b319ee637b1b0a5bcfafe1597cb490299bbbc6329456",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=["ha-mcp-web"],
            ports=[_UPSTREAM.container_port()],
            env_from=[EnvFrom(config_map=config_map)],
            env_variables={
                "HOMEASSISTANT_TOKEN": _HOME_ASSISTANT_TOKEN.env_value(self, "ha-mcp-home-assistant-token-ref")
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(500)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.gibibytes(1)),
            ),
            readiness=http_probe("/healthz", port=_UPSTREAM.number, initial_delay_seconds=5),
            liveness=http_probe("/healthz", port=_UPSTREAM.number, initial_delay_seconds=20, period_seconds=20),
            security_context=ContainerSecurityContextProps(
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]), user=999, group=999
            ),
        )
        deployment.containers[0].mount("/tmp", tmp_volume)
        deployment.containers[0].mount(_APP_DATA_DIR, data_volume)

        deployment.add_container(
            name="facade",
            image=f"{_APP_FACADE_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            ports=[FACADE.port.container_port(), _METRICS.port.container_port()],
            env_from=[EnvFrom(config_map=config_map)],
            env_variables={
                # The same token agentplane-staging's Action Service presents (its ESO copy of
                # this Secret) -- one source of truth, no drift.
                "MCP_FACADE_CLIENT_AUTH__STATIC_BEARER": BEARER.env_value(self, "ha-mcp-bearer-ref")
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(200)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(256)),
            ),
            readiness=http_probe("/healthz", port=FACADE.pod_port, initial_delay_seconds=5),
            liveness=http_probe("/healthz", port=FACADE.pod_port, initial_delay_seconds=20, period_seconds=20),
            # cdk8s_plus_34 defaults containers to a hardened SecurityContext
            # (readOnlyRootFilesystem/runAsNonRoot: true). Opt out explicitly to preserve
            # today's actual (unrestricted) behavior -- the real container's
            # filesystem-write/root needs were never audited, so silently hardening it here
            # could break the running facade. Same rationale as litellm/proxy.py.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False, ensure_non_root=False),
        )
        pod_policy.harden(deployment)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=FACADE.name, namespace=_NAMESPACE, labels=FACADE.labels),
            selector=deployment,
            ports=[FACADE.port.service_port(), _METRICS.port.service_port()],
        )

    def _add_network_policy(self) -> None:
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(
                name="ingress",
                namespace=_NAMESPACE,
                annotations={
                    "description": (
                        "Default-deny ingress for HA-MCP. Only agentplane-staging reaches the facade port; the "
                        "upstream server port is reachable only over pod-local loopback. No Gateway ingress -- "
                        "this MCP is cluster-internal since the move to a static bearer."
                    )
                },
            ),
            endpoint_selector=FACADE.pods.selector,
            ingress=[
                IngressRule.from_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": "agentplane-staging"}, ports=[FACADE.pod_port]
                ),
                cilium.SCRAPERS.admit(_METRICS.pod_port),
            ],
        )

    def _add_service_monitor(self) -> None:
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=ApiObjectMetadata(name=_APP_NAME, namespace=_NAMESPACE),
            selector=ServiceMonitorSpecSelector(match_labels=FACADE.labels),
            endpoints=[Endpoint.plain(port=_METRICS.port.name)],
        )


class HaMcp(Construct):
    """The whole ha-mcp Kustomization: its Namespace, its Home Assistant token, and the app."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        namespaces.namespace(
            self, "namespace", name=_NAMESPACE, vpa=Vpa.DISABLED, labels={"app.kubernetes.io/name": _NAMESPACE}
        )
        _home_assistant_token(self)
        HaMcpApp(self, "app")


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    HaMcp(chart, _NAME)
    add_fleet_rules(chart)
    return chart


def ha_mcp(
    flux_chart: Chart,
    directory: RenderedDirectory,
    external_secrets_operator: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        _NAME,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_operator,
            # the ServiceMonitor CRD
            monitoring_crds,
        ),
    )
