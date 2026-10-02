"""The ssh-mcp Deployment and its generated configuration and credentials."""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, JsonPatch, Size
from cdk8s_plus_34 import (
    Capability,
    ConfigMap,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    EnvValue,
    ImagePullPolicy,
    LabelSelector,
    MemoryResources,
    PodSecurityContextProps,
    Service,
    Volume,
    k8s,
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress
from constructs import Construct
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import cilium, namespaces, pod_policy, public_coder_devbox
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret, forgejo_images_creds_secret_ref
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.ssh_mcp.config import (
    BEARER_SECRET_KEY,
    BEARER_SECRET_NAME,
    CONFIG_DIR,
    CONFIG_MAP_NAME,
    NAME,
    NAMESPACE,
    SERVICE,
    SshMcpConfig,
)
from cluster.scripts.nebula_mesh import Mesh

IMAGE_NAME = "git.allegedly.works/ducktape-ci/ssh-mcp"
PLACEHOLDER_TAG = "unset"
_BEARER = SecretRef(namespace=NAMESPACE, name=BEARER_SECRET_NAME).key(BEARER_SECRET_KEY)
_KEY_VOLUMES = (
    ("keys", "ssh-mcp-keys", f"{CONFIG_DIR}/keys"),
    ("keys-public-coder-devbox", "ssh-mcp-keys-public-coder-devbox", f"{CONFIG_DIR}/keys-public-coder-devbox"),
    ("keys-atlas", "ssh-mcp-keys-atlas", f"{CONFIG_DIR}/keys-atlas"),
)


def _config_map(scope: Construct, config: SshMcpConfig) -> ConfigMap:

    return ConfigMap(
        scope,
        "configuration",
        metadata=ApiObjectMetadata(name=CONFIG_MAP_NAME, namespace=NAMESPACE),
        data={"settings.yaml": yaml_config(config.settings), "known_hosts": config.known_hosts},
    )


def _egress(mesh: Mesh, config: SshMcpConfig) -> list[CiliumNetworkPolicySpecEgress]:
    rules = [
        cilium.dns_egress(),
        # Cluster nodes use Cilium's host/remote-node identities. A CIDR rule does not
        # select their Nebula IPs unless policy-cidr-match-mode:nodes is enabled.
        EgressRule.to_entities(Entity.HOST, Entity.REMOTE_NODE, ports=[22]),
        # The devbox is an ordinary pod, while non-Kubernetes Nebula peers need to be
        # selected by CIDR. Their membership and addresses come from nebula-mesh.json.
        public_coder_devbox.SSH.egress(),
    ]
    external_ips = [
        f"{mesh.hosts[hostname].nebula_ip}/32"
        for hostname in config.nebula_hostnames
        if mesh.hosts[hostname].role == "non-k8s"
    ]
    if external_ips:
        rules.append(EgressRule.to_cidrs(*external_ips, ports=[22]))
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
            name=_BEARER.secret.name,
            namespace=_BEARER.secret.namespace,
            key=_BEARER.key,
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        )

    def _add_deployment(self, config_map: ConfigMap, *, config: SshMcpConfig, mesh: Mesh) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE, labels=SERVICE.pods.selector),
            pod_metadata=ApiObjectMetadata(labels=SERVICE.pods.selector),
            replicas=1,
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
            image=f"{IMAGE_NAME}:{PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            env_variables={
                "SSH_MCP_CONFIG_FILE": EnvValue.from_value(f"{CONFIG_DIR}/settings.yaml"),
                "SSH_MCP_BEARER_TOKEN": _BEARER.env_value(self, "bearer-secret-ref"),
                "SSH_MCP_HOST": EnvValue.from_value("0.0.0.0"),
                "SSH_MCP_PORT": EnvValue.from_value(str(SERVICE.pod_port)),
            },
            ports=[SERVICE.port.container_port()],
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(500)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            readiness=http_probe("/healthz", port=SERVICE.pod_port, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=SERVICE.pod_port, initial_delay_seconds=15, period_seconds=20),
            security_context=ContainerSecurityContextProps(
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                ensure_non_root=True,
                user=1000,
                group=1000,
                # The aspect_rules_py launcher materializes its venv in the image's runfiles.
                read_only_root_filesystem=False,
            ),
        )
        config_volume = Volume.from_config_map(self, "config-volume", config_map)
        deployment.containers[0].mount(CONFIG_DIR, config_volume, read_only=True)
        # cdk8s_plus has no optional SecretVolumeSource builder. Keep the optionality in
        # typed Kubernetes structs: target keys may be absent while the service stays up.
        for name, secret, mount_path in _KEY_VOLUMES:
            ApiObject.of(deployment).add_json_patch(
                JsonPatch.add(
                    "/spec/template/spec/volumes/-",
                    k8s.Volume(name=name, secret=k8s.SecretVolumeSource(secret_name=secret, optional=True)),
                )
            )
            ApiObject.of(deployment).add_json_patch(
                JsonPatch.add(
                    "/spec/template/spec/containers/0/volumeMounts/-",
                    k8s.VolumeMount(name=name, mount_path=mount_path, read_only=True),
                )
            )
        ApiObject.of(deployment).add_json_patch(
            JsonPatch.add(
                "/spec/template/spec/hostAliases",
                [
                    k8s.HostAlias(ip=mesh.hosts[hostname].nebula_ip, hostnames=[f"{hostname}.nebula.allegedly.works"])
                    for hostname in config.nebula_hostnames
                ],
            )
        )
        pod_policy.harden(deployment)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=SERVICE.name, namespace=NAMESPACE),
            selector=deployment,
            ports=[SERVICE.port.service_port()],
        )

    def _add_network_policy(self, config: SshMcpConfig, mesh: Mesh) -> None:
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
            endpoint_selector=SERVICE.pods.selector,
            ingress=[
                IngressRule.from_endpoints(
                    cilium.endpoint_labels("agentplane-staging", "agentplane-actions"), ports=[SERVICE.pod_port]
                )
            ],
            egress=_egress(mesh, config),
        )


def chart(app: App, *, config: SshMcpConfig, mesh: Mesh) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.DISABLED,
        labels={"name": NAMESPACE},
        annotations={"description": "SSH MCP backend; private keys stay in this namespace."},
    )
    SshMcp(chart, NAME, config=config, mesh=mesh)
    return chart
