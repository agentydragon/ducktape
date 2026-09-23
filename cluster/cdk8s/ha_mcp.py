"""Everything ha-mcp needs, generated into one Kustomization: its Namespace, the ESO copy of
the Home Assistant token the Home Assistant provisioner keeps valid (cluster/cdk8s/home_assistant),
and the ConfigMap/Deployment/Service/CiliumNetworkPolicy/ServiceMonitor for the MCP server itself.
cdk8s generates all of it, so there's no real need to keep the Namespace/credentials/app split
into separate directories the way hand-written manifests once did -- see cluster/docs/cdk8s.md.

The facade container's image tag is a deliberate placeholder ("unset") --
image-pins/kustomization.yaml (hand-written, see
cluster/k8s/agents/ha-mcp/app/image-pins/kustomization.yaml) overrides it at
`kustomize build` time via Flux's image-automation marker. The ha-mcp container's own
image is pinned by digest directly and isn't Flux-managed.

The facade's static bearer token is minted by ESO's Password generator (same pattern
as ssh_mcp/backend.py's `_bearer_credentials`), not hand-written SOPS -- ducktape mints
this value itself, so there is no ciphertext to keep in sync with the cluster's age
recipients.
"""

from __future__ import annotations

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
    EnvFrom,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    Namespace,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServiceAccount,
    ServicePort,
    Volume,
)
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import Kustomization
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecSelector,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import cilium
from cluster.cdk8s.external_secrets.external_secret import (
    add_external_secret,
    cluster_secret_store,
    password_generator,
    remote_data,
)
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import flux_kustomization, flux_kustomization_depends_on_many, kustomize_kustomization
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret, forgejo_images_creds_secret_ref
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.home_assistant.app import HA_MCP_TOKEN
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch
from cluster.cdk8s.probes import http_probe

_NAMESPACE = "ha-mcp"
OUTPUT_DIR = "cluster/k8s/agents/ha-mcp/app"
_HOME_ASSISTANT_TOKEN_SECRET_NAME = "ha-mcp-home-assistant-token"
_BEARER_SECRET_NAME = "ha-mcp-bearer"
_BEARER_SECRET_KEY = "bearer-token"
_PLACEHOLDER_TAG = "unset"

_APP_NAME = "ha-mcp"
_APP_FACADE_IMAGE_NAME = "git.allegedly.works/ducktape-ci/mcp-oauth-facade"
_APP_CONFIG_MAP_NAME = "ha-mcp-config"
_APP_UPSTREAM_PORT = 8086
_APP_FACADE_PORT = 8765
_APP_METRICS_PORT = 9090
_APP_LABELS = {"app.kubernetes.io/name": _APP_NAME}
_APP_DATA_DIR = "/data"


def _bearer_credentials(scope: Construct) -> None:
    """The facade's static bearer token: ducktape mints it itself (same pattern as
    ssh_mcp/backend.py's `_bearer_credentials`), so ESO's Password generator creates it
    directly -- no hand-written SOPS ciphertext to keep in sync with cluster recipients.

    agentplane-staging copies it with ESO through a store that can read this one Secret
    (cluster/cdk8s/agentplane/staging.py): this namespace also holds the Home Assistant admin
    token, which no store may reach.
    """
    Password(
        scope,
        "bearer-password-generator",
        metadata=metadata(_BEARER_SECRET_NAME, _NAMESPACE),
        spec=PasswordSpec(length=48, digits=12, symbols=0, no_upper=False, allow_repeat=True),
    )
    add_external_secret(
        scope,
        "bearer-external-secret",
        name=_BEARER_SECRET_NAME,
        namespace=_NAMESPACE,
        refresh=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
        data_from=[password_generator(_BEARER_SECRET_NAME)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(type="Opaque", data={_BEARER_SECRET_KEY: "{{ .password }}"}),
    )


def _home_assistant_token(scope: Construct) -> None:
    """ESO copy of the token the Home Assistant provisioner keeps valid in its own namespace, read
    through a store that can get that one Secret."""
    reader = ServiceAccount(
        scope, "home-assistant-token-reader", metadata=metadata("home-assistant-token-reader", _NAMESPACE)
    )
    add_external_secret(
        scope,
        "home-assistant-token",
        name=_HOME_ASSISTANT_TOKEN_SECRET_NAME,
        namespace=_NAMESPACE,
        refresh="1h",
        store=cluster_secret_store(
            single_secret_store(
                scope,
                "ha-mcp-home-assistant-token",
                reader=reader,
                source_namespace=HA_MCP_TOKEN.secret_namespace,
                source_secret=HA_MCP_TOKEN.secret_name,
                consumer_namespace=_NAMESPACE,
            )
        ),
        data=[remote_data(HA_MCP_TOKEN.secret_name, "token")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )


class HaMcpApp(Construct):
    """The ha-mcp Deployment/Service/ConfigMap/NetworkPolicy/ServiceMonitor and its pull credentials."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=_NAMESPACE)
        _bearer_credentials(self)
        config_map = self._add_config_map()
        deployment = self._add_deployment(config_map)
        self._add_service(deployment)
        self._add_network_policy()
        self._add_service_monitor()

    def _add_config_map(self) -> ConfigMap:
        return ConfigMap(
            self,
            "config",
            metadata=metadata(_APP_CONFIG_MAP_NAME, _NAMESPACE),
            data={
                "HOMEASSISTANT_URL": "http://home-assistant.home-assistant.svc.cluster.local:8123",
                "MCP_HOST": "0.0.0.0",
                "MCP_PORT": str(_APP_UPSTREAM_PORT),
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
                "MCP_FACADE_UPSTREAM__URL": f"http://localhost:{_APP_UPSTREAM_PORT}/mcp",
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
            metadata=metadata(
                _APP_NAME,
                _NAMESPACE,
                labels=_APP_LABELS,
                annotations={
                    "description": (
                        "Writable Home Assistant MCP server behind the shared MCP facade, cluster-internal "
                        "and gated by a static bearer that only agentplane-staging's Action Service holds. "
                        "The upstream HA token remains server-side, and the Action Service applies its own "
                        "per-call approval policy."
                    ),
                    "reloader.stakater.com/auto": "true",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_APP_LABELS),
            replicas=1,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            automount_service_account_token=False,
        )
        ApiObject.of(deployment).add_json_patch(runtime_default_seccomp_patch())

        tmp_volume = Volume.from_empty_dir(self, "tmp-volume", "tmp")
        data_volume = Volume.from_empty_dir(self, "data-volume", "data")

        deployment.add_container(
            name="ha-mcp",
            image="ghcr.io/homeassistant-ai/ha-mcp:8.4.3@sha256:d5cea47a0115e5d161c2b319ee637b1b0a5bcfafe1597cb490299bbbc6329456",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=["ha-mcp-web"],
            ports=[ContainerPort(name="upstream", number=_APP_UPSTREAM_PORT, protocol=Protocol.TCP)],
            env_from=[EnvFrom(config_map=config_map)],
            env_variables={
                "HOMEASSISTANT_TOKEN": EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(
                            self, "ha-mcp-home-assistant-token-ref", _HOME_ASSISTANT_TOKEN_SECRET_NAME
                        ),
                        key="token",
                    )
                )
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(500)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.gibibytes(1)),
            ),
            readiness=http_probe("/healthz", port=_APP_UPSTREAM_PORT, initial_delay_seconds=5),
            liveness=http_probe("/healthz", port=_APP_UPSTREAM_PORT, initial_delay_seconds=20, period_seconds=20),
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
            ports=[
                ContainerPort(name="http", number=_APP_FACADE_PORT, protocol=Protocol.TCP),
                ContainerPort(name="metrics", number=_APP_METRICS_PORT, protocol=Protocol.TCP),
            ],
            env_from=[EnvFrom(config_map=config_map)],
            env_variables={
                # The same token agentplane-staging's Action Service presents (its ESO copy of
                # this Secret) -- one source of truth, no drift.
                "MCP_FACADE_CLIENT_AUTH__STATIC_BEARER": EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(self, "ha-mcp-bearer-ref", _BEARER_SECRET_NAME),
                        key=_BEARER_SECRET_KEY,
                    )
                )
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(200)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(256)),
            ),
            readiness=http_probe("/healthz", port=_APP_FACADE_PORT, initial_delay_seconds=5),
            liveness=http_probe("/healthz", port=_APP_FACADE_PORT, initial_delay_seconds=20, period_seconds=20),
            # cdk8s_plus_34 defaults containers to a hardened SecurityContext
            # (readOnlyRootFilesystem/runAsNonRoot: true). Opt out explicitly to preserve
            # today's actual (unrestricted) behavior -- the real container's
            # filesystem-write/root needs were never audited, so silently hardening it here
            # could break the running facade. Same rationale as litellm/proxy.py.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False, ensure_non_root=False),
        )
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(_APP_NAME, _NAMESPACE, labels=_APP_LABELS),
            selector=deployment,
            ports=[
                ServicePort(name="http", port=_APP_FACADE_PORT, target_port=_APP_FACADE_PORT, protocol=Protocol.TCP),
                ServicePort(
                    name="metrics", port=_APP_METRICS_PORT, target_port=_APP_METRICS_PORT, protocol=Protocol.TCP
                ),
            ],
        )

    def _add_network_policy(self) -> None:
        cilium.network_policy(
            self,
            "networkpolicy",
            metadata=metadata(
                "ha-mcp-ingress",
                _NAMESPACE,
                annotations={
                    "description": (
                        "Default-deny ingress for HA-MCP. Only agentplane-staging reaches the facade port; the "
                        "upstream server port is reachable only over pod-local loopback. No Gateway ingress -- "
                        "this MCP is cluster-internal since the move to a static bearer."
                    )
                },
            ),
            selector=_APP_LABELS,
            ingress=[
                cilium.ingress_from(
                    {"k8s:io.kubernetes.pod.namespace": "agentplane-staging"}, ports=[_APP_FACADE_PORT]
                ),
                cilium.ingress_from({"k8s:io.kubernetes.pod.namespace": "monitoring"}, ports=[_APP_METRICS_PORT]),
            ],
        )

    def _add_service_monitor(self) -> None:
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=metadata(_APP_NAME, _NAMESPACE),
            spec=ServiceMonitorSpec(
                selector=ServiceMonitorSpecSelector(match_labels=_APP_LABELS),
                endpoints=[ServiceMonitorSpecEndpoints(port="metrics")],
            ),
        )


class HaMcp(Construct):
    """The whole ha-mcp Kustomization: its Namespace, its Home Assistant token, and the app."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        Namespace(
            self,
            "namespace",
            metadata=ApiObjectMetadata(
                name=_NAMESPACE,
                labels={"app.kubernetes.io/name": _NAMESPACE, "goldilocks.fairwinds.com/enabled": "false"},
            ),
        )
        _home_assistant_token(self)
        HaMcpApp(self, "app")


def ha_mcp(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    root: Path,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    home_assistant: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "ha-mcp"
    app_dir = root / OUTPUT_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(app_dir))
    chart = Chart(app, name, disable_resource_name_hashes=True)
    HaMcp(chart, "ha-mcp")
    add_fleet_rules(chart)
    app.synth()

    kustomization = flux_kustomization(
        flux_chart,
        name,
        artifact,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_config,
            forgejo_images,
            home_assistant,
            # the ServiceMonitor CRD
            monitoring_crds,
        ),
    )
    write_yaml(
        app_dir / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{name}.k8s.yaml"], components=["./image-pins"]),
    )
    return kustomization
