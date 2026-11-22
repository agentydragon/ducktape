"""The ssh-mcp Deployment and its generated configuration and credentials."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    EnvValue,
    HostAlias,
    ImagePullPolicy,
    LabelSelector,
    MemoryResources,
    PodSecurityContextProps,
    Secret,
    Service,
    Volume,
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress
from constructs import Construct
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import cilium, namespaces, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import actions
from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.forgejo_registry.pull_credentials import (
    forgejo_images_creds_external_secret,
    forgejo_images_creds_secret_ref,
)
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.ssh_mcp.config import (
    BEARER,
    CONFIG_DIR,
    CONFIG_MAP_NAME,
    NAME,
    NAMESPACE,
    SERVICE,
    LanDestination,
    NebulaDestination,
    SshMcpConfig,
)
from cluster.cdk8s.vpa_policy import ContainerPolicy
from cluster.scripts.nebula_mesh import Mesh

IMAGE_NAME = "git.allegedly.works/ducktape-ci/ssh-mcp"
PLACEHOLDER_TAG = "unset"
_CPU_REQUEST_MILLIS = 50
_MEMORY_LIMIT_MIB = 512
_KEY_VOLUMES = (
    ("keys", "ssh-mcp-keys", f"{CONFIG_DIR}/keys"),
    ("keys-atlas", "ssh-mcp-keys-atlas", f"{CONFIG_DIR}/keys-atlas"),
    ("keys-agentplane", "ssh-mcp-keys-agentplane", f"{CONFIG_DIR}/keys-agentplane"),
)


def _config_map(scope: Construct, config: SshMcpConfig) -> ConfigMap:

    return ConfigMap(
        scope,
        "configuration",
        metadata=ApiObjectMetadata(name=CONFIG_MAP_NAME, namespace=NAMESPACE),
        data={"settings.yaml": yaml_config(config.settings), "known_hosts": config.known_hosts},
    )


def _egress(mesh: Mesh, config: SshMcpConfig) -> list[CiliumNetworkPolicySpecEgress]:
    destinations = config.destinations
    rules = [cilium.dns_egress()]
    # Cilium treats cluster nodes as host/remote-node entities. A CIDR rule does not
    # select their Nebula IPs unless policy-cidr-match-mode:nodes is enabled, so this
    # permits SSH to all cluster nodes whenever any configured Nebula target is one.
    if any(
        isinstance(destination, NebulaDestination) and mesh.hosts[destination.name].role != "non-k8s"
        for destination in destinations
    ):
        rules.append(EgressRule.to_entities(Entity.HOST, Entity.REMOTE_NODE, ports=[22]))

    nebula_ips = [
        f"{mesh.hosts[hostname].nebula_ip}/32"
        for hostname in dict.fromkeys(
            destination.name for destination in destinations if isinstance(destination, NebulaDestination)
        )
        if mesh.hosts[hostname].role == "non-k8s"
    ]
    if nebula_ips:
        rules.append(EgressRule.to_cidrs(*nebula_ips, ports=[22]))

    lan_ips = [
        f"{destination.address}/{destination.address.max_prefixlen}"
        for destination in destinations
        if isinstance(destination, LanDestination)
    ]
    if lan_ips:
        rules.append(EgressRule.to_cidrs(*lan_ips, ports=[22]))
    return rules


class SshMcp(Construct):
    """Backend service, ConfigMap, ESO credentials, and Cilium policy."""

    def __init__(self, scope: Construct, id: str, *, config: SshMcpConfig, mesh: Mesh) -> None:
        super().__init__(scope, id)
        self._add_credentials()
        config_map = _config_map(self, config)
        deployment = self._add_deployment(config_map, config=config, mesh=mesh)
        self._add_service(deployment)
        self._add_network_policy(config, mesh)
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=NAMESPACE)

    def _add_credentials(self) -> None:
        # agentplane-staging copies it with ESO through a store that can read this one Secret
        # (cluster/cdk8s/agentplane/staging.py): this namespace also holds every target's SSH
        # private key, which no store may reach.
        mint_bearer_secret(
            self,
            "bearer-external-secret",
            name=BEARER.secret.name,
            namespace=BEARER.secret.namespace,
            key=BEARER.key,
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        )

    def _add_deployment(self, config_map: ConfigMap, *, config: SshMcpConfig, mesh: Mesh) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=NAME,
                namespace=NAMESPACE,
                labels=SERVICE.pods.selector,
                annotations=namespaces.initial_request_annotations(
                    # Peak 30-day working set was 123Mi.
                    ContainerPolicy.cpu_and_memory_requests_only("server", memory_floor="144Mi")
                ),
            ),
            pod_metadata=ApiObjectMetadata(labels=SERVICE.pods.selector),
            replicas=1,
            select=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            host_aliases=[
                HostAlias(ip=mesh.hosts[destination.name].nebula_ip, hostnames=[destination.host])
                for destination in config.destinations
                if isinstance(destination, NebulaDestination)
            ],
            automount_service_account_token=False,
            enable_service_links=False,
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
        )
        # The Deployment selector is immutable; retain its existing labels for Flux adoption.
        deployment.select(LabelSelector.of(labels=SERVICE.pods.selector))
        server = deployment.add_container(
            name="server",
            image=f"{IMAGE_NAME}:{PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            env_variables={
                "SSH_MCP_CONFIG_FILE": EnvValue.from_value(f"{CONFIG_DIR}/settings.yaml"),
                "SSH_MCP_BEARER_TOKEN": BEARER.env_value(self, "bearer-secret-ref"),
                "SSH_MCP_HOST": EnvValue.from_value("0.0.0.0"),
                "SSH_MCP_PORT": EnvValue.from_value(str(SERVICE.pod_port)),
            },
            ports=[SERVICE.container_port()],
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
        # The home switch is reachable from the home-LAN node. Keep the backend
        # there so its egress follows the same routed path as the tofu runner.
        pod_policy.place(deployment, node_scheduling.Placement(node_selector=node_scheduling.HOME_LAN_NODE_SELECTOR))
        config_volume = Volume.from_config_map(self, "config-volume", config_map)
        server.mount(CONFIG_DIR, config_volume, read_only=True)
        # Targets can acquire their keys after the server starts.
        for name, secret_name, mount_path in _KEY_VOLUMES:
            secret = Secret.from_secret_name(self, f"{name}-secret", secret_name)
            volume = Volume.from_secret(self, name, secret, name=name, optional=True)
            server.mount(mount_path, volume, read_only=True)
        pod_policy.harden(deployment)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=SERVICE.name, namespace=NAMESPACE),
            selector=deployment,
            ports=[SERVICE.service_port()],
        )

    def _add_network_policy(self, config: SshMcpConfig, mesh: Mesh) -> None:
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
            endpoint_selector=SERVICE.pods.selector,
            ingress=[actions.service(STAGING_NAMESPACE).pods.admit(SERVICE.pod_port)],
            egress=_egress(mesh, config),
        )


def chart(app: App, *, config: SshMcpConfig, mesh: Mesh) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.RECOMMEND,
        labels={"name": NAMESPACE},
        annotations={"description": "SSH MCP backend; private keys stay in this namespace."},
    )
    SshMcp(chart, NAME, config=config, mesh=mesh)
    return chart
