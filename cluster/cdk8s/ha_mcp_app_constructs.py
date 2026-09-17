"""ConfigMap + Deployment + Service + CiliumNetworkPolicy + ServiceMonitor for
ha-mcp itself: a writable Home Assistant MCP server behind the shared MCP
facade, cluster-internal and gated by a static bearer only haku-console holds.

The facade container's image tag is a deliberate placeholder ("unset") --
image-pins/kustomization.yaml (hand-written, see
cluster/k8s/agents/ha-mcp/app/image-pins/kustomization.yaml) overrides it at
`kustomize build` time via Flux's image-automation marker. The ha-mcp
container's own image is pinned by digest directly and isn't Flux-managed.
See cluster/docs/cdk8s.md.

bearer.sops.yaml (the static bearer token consumed by the facade container)
stays hand-written alongside this generated output -- cdk8s has no key
material to synthesize ciphertext with. See cluster/docs/cdk8s.md § SOPS
secrets in a converted directory.
"""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, Duration, JsonPatch, Size
from cdk8s_plus_33 import (
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
    Probe,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServicePort,
    Volume,
)
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret

_NAME = "ha-mcp"
_NAMESPACE = "ha-mcp"
_FACADE_IMAGE_NAME = "git.allegedly.works/ducktape-ci/mcp-oauth-facade"
_PLACEHOLDER_TAG = "unset"
_CONFIG_MAP_NAME = "ha-mcp-config"

_UPSTREAM_PORT = 8086
_FACADE_PORT = 8765
_METRICS_PORT = 9090

_LABELS = {"app.kubernetes.io/name": _NAME}


def _metadata(
    name: str,
    *,
    namespace: str = _NAMESPACE,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> ApiObjectMetadata:
    return ApiObjectMetadata(name=name, namespace=namespace, labels=labels, annotations=annotations)


class HaMcpApp(Construct):
    """The ha-mcp Deployment/Service/ConfigMap/NetworkPolicy/ServiceMonitor and its pull credentials."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=_NAMESPACE)
        config_map = self._add_config_map()
        deployment = self._add_deployment(config_map)
        self._add_service(deployment)
        self._add_network_policy()
        self._add_service_monitor()

    def _add_config_map(self) -> ConfigMap:
        return ConfigMap(
            self,
            "config",
            metadata=_metadata(_CONFIG_MAP_NAME),
            data={
                "HOMEASSISTANT_URL": "http://home-assistant.home-assistant.svc.cluster.local:8123",
                "MCP_HOST": "0.0.0.0",
                "MCP_PORT": str(_UPSTREAM_PORT),
                "MCP_SECRET_PATH": "/mcp",
                "MCP_HEALTHZ": "true",
                "MCP_SERVER_NAME": "Home Assistant",
                "ENVIRONMENT": "production",
                "LOG_LEVEL": "INFO",
                "BACKUP_HINT": "normal",
                "HA_MCP_CONFIG_DIR": "/data",
                "HAMCP_BACKUP_DIR": "/data/backups",
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
                "MCP_FACADE_UPSTREAM__URL": f"http://localhost:{_UPSTREAM_PORT}/mcp",
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
            metadata=_metadata(
                _NAME,
                labels=_LABELS,
                annotations={
                    "description": (
                        "Writable Home Assistant MCP server behind the shared MCP facade, cluster-internal "
                        "and gated by a static bearer that only haku-console holds. The upstream HA token "
                        "remains server-side, and Haku applies its own per-call approval policy."
                    ),
                    "reloader.stakater.com/auto": "true",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=1,
            docker_registry_auth=Secret.from_secret_name(self, "forgejo-images-creds-ref", "forgejo-images-creds"),
            automount_service_account_token=False,
        )

        tmp_volume = Volume.from_empty_dir(self, "tmp-volume", "tmp")
        data_volume = Volume.from_empty_dir(self, "data-volume", "data")

        deployment.add_container(
            name="ha-mcp",
            image="ghcr.io/homeassistant-ai/ha-mcp:8.4.3@sha256:d5cea47a0115e5d161c2b319ee637b1b0a5bcfafe1597cb490299bbbc6329456",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=["ha-mcp-web"],
            ports=[ContainerPort(name="upstream", number=_UPSTREAM_PORT, protocol=Protocol.TCP)],
            env_from=[EnvFrom(config_map=config_map)],
            env_variables={
                "HOMEASSISTANT_TOKEN": EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(
                            self, "ha-mcp-home-assistant-token-ref", "ha-mcp-home-assistant-token"
                        ),
                        key="token",
                    )
                )
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(500)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.gibibytes(1)),
            ),
            readiness=Probe.from_http_get(
                "/healthz",
                port=_UPSTREAM_PORT,
                initial_delay_seconds=Duration.seconds(5),
                period_seconds=Duration.seconds(10),
            ),
            liveness=Probe.from_http_get(
                "/healthz",
                port=_UPSTREAM_PORT,
                initial_delay_seconds=Duration.seconds(20),
                period_seconds=Duration.seconds(20),
            ),
            security_context=ContainerSecurityContextProps(
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]), user=999, group=999
            ),
        )
        deployment.containers[0].mount("/tmp", tmp_volume)
        deployment.containers[0].mount("/data", data_volume)

        deployment.add_container(
            name="facade",
            image=f"{_FACADE_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            ports=[
                ContainerPort(name="http", number=_FACADE_PORT, protocol=Protocol.TCP),
                ContainerPort(name="metrics", number=_METRICS_PORT, protocol=Protocol.TCP),
            ],
            env_from=[EnvFrom(config_map=config_map)],
            env_variables={
                # The same token haku-console presents (reflected as ha-mcp-bearer into
                # haku-console by the emberstack reflector) -- one source of truth, no drift.
                "MCP_FACADE_CLIENT_AUTH__STATIC_BEARER": EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(self, "ha-mcp-bearer-ref", "ha-mcp-bearer"), key="bearer-token"
                    )
                )
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(200)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(256)),
            ),
            readiness=Probe.from_http_get(
                "/healthz",
                port=_FACADE_PORT,
                initial_delay_seconds=Duration.seconds(5),
                period_seconds=Duration.seconds(10),
            ),
            liveness=Probe.from_http_get(
                "/healthz",
                port=_FACADE_PORT,
                initial_delay_seconds=Duration.seconds(20),
                period_seconds=Duration.seconds(20),
            ),
            # cdk8s_plus_33 defaults containers to a hardened SecurityContext
            # (readOnlyRootFilesystem/runAsNonRoot: true). Opt out explicitly to preserve
            # today's actual (unrestricted) behavior -- the real container's
            # filesystem-write/root needs were never audited, so silently hardening it here
            # could break the running facade. Same rationale as litellm_constructs.py.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False, ensure_non_root=False),
        )
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=_metadata(_NAME, labels=_LABELS),
            selector=deployment,
            ports=[
                ServicePort(name="http", port=_FACADE_PORT, target_port=_FACADE_PORT, protocol=Protocol.TCP),
                ServicePort(name="metrics", port=_METRICS_PORT, target_port=_METRICS_PORT, protocol=Protocol.TCP),
            ],
        )

    def _add_network_policy(self) -> None:
        # cdk8s_plus_33 has no typed builder for Cilium CRDs (no third_party/cilium typed
        # bindings exist yet) -- raw ApiObject + JsonPatch, same escape hatch as the Role in
        # ha_mcp_credentials_constructs.py.
        netpol = ApiObject(
            self,
            "networkpolicy",
            api_version="cilium.io/v2",
            kind="CiliumNetworkPolicy",
            metadata=_metadata(
                "ha-mcp-ingress",
                annotations={
                    "description": (
                        "Default-deny ingress for HA-MCP. Only haku-console reaches the facade port; the "
                        "upstream server port is reachable only over pod-local loopback. No Gateway ingress "
                        "-- this MCP is cluster-internal since the move to a static bearer."
                    )
                },
            ),
        )
        netpol.add_json_patch(
            JsonPatch.add(
                "/spec",
                {
                    "endpointSelector": {"matchLabels": _LABELS},
                    "ingress": [
                        {
                            "fromEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "haku-console"}}],
                            "toPorts": [{"ports": [{"port": str(_FACADE_PORT), "protocol": "TCP"}]}],
                        },
                        {
                            "fromEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "monitoring"}}],
                            "toPorts": [{"ports": [{"port": str(_METRICS_PORT), "protocol": "TCP"}]}],
                        },
                    ],
                },
            )
        )

    def _add_service_monitor(self) -> None:
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=_metadata(_NAME),
            spec=ServiceMonitorSpec(
                selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
                endpoints=[ServiceMonitorSpecEndpoints(port="metrics", interval="30s")],
            ),
        )
