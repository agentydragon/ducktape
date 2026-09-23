"""The ssh-mcp Deployment and its generated configuration and credentials."""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, JsonPatch, Size
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
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    LabelSelector,
    MemoryResources,
    PodSecurityContextProps,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServicePort,
    Volume,
    k8s,
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)

from cluster.cdk8s import cilium
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.external_secrets.external_secret import add_external_secret, password_generator
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret, forgejo_images_creds_secret_ref
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.ssh_mcp.config import (
    BEARER_SECRET_KEY,
    BEARER_SECRET_NAME,
    CONFIG_DIR,
    CONFIG_MAP_NAME,
    HTTP_PORT,
    NAME,
    NAMESPACE,
    SshMcpConfig,
)
from cluster.scripts.nebula_mesh import Mesh

IMAGE_NAME = "git.allegedly.works/ducktape-ci/ssh-mcp"
PLACEHOLDER_TAG = "unset"
LABELS = {"app.kubernetes.io/name": NAME}
_KEY_VOLUMES = (
    ("keys", "ssh-mcp-keys", f"{CONFIG_DIR}/keys"),
    ("keys-public-coder-devbox", "ssh-mcp-keys-public-coder-devbox", f"{CONFIG_DIR}/keys-public-coder-devbox"),
    ("keys-atlas", "ssh-mcp-keys-atlas", f"{CONFIG_DIR}/keys-atlas"),
)


def _bearer_credentials(scope: Construct) -> None:
    Password(
        scope,
        "bearer-password-generator",
        metadata=metadata(BEARER_SECRET_NAME, NAMESPACE),
        spec=PasswordSpec(length=48, digits=12, symbols=0, no_upper=False, allow_repeat=True),
    )
    add_external_secret(
        scope,
        "bearer-external-secret",
        name=BEARER_SECRET_NAME,
        namespace=NAMESPACE,
        refresh=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
        data_from=[password_generator(BEARER_SECRET_NAME)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            type="Opaque",
            metadata=ExternalSecretSpecTargetTemplateMetadata(
                annotations={
                    "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                    "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "^agentplane-staging$",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "^agentplane-staging$",
                }
            ),
            data={BEARER_SECRET_KEY: "{{ .password }}"},
        ),
    )


def _config_map(scope: Construct, config: SshMcpConfig) -> ConfigMap:

    return ConfigMap(
        scope,
        "configuration",
        metadata=metadata(CONFIG_MAP_NAME, NAMESPACE),
        data={"settings.yaml": yaml_config(config.settings), "known_hosts": config.known_hosts},
    )


def _egress(mesh: Mesh, config: SshMcpConfig) -> list[CiliumNetworkPolicySpecEgress]:
    rules = [
        cilium.dns_egress(),
        # Cluster nodes use Cilium's host/remote-node identities. A CIDR rule does not
        # select their Nebula IPs unless policy-cidr-match-mode:nodes is enabled.
        cilium.egress_to_entities("host", "remote-node", ports=[22]),
        # The devbox is an ordinary pod, while non-Kubernetes Nebula peers need to be
        # selected by CIDR. Their membership and addresses come from nebula-mesh.json.
        cilium.egress_to(cilium.endpoint_labels("public-coder-agent", "public-coder-devbox"), 22),
    ]
    external_ips = [
        f"{mesh.hosts[hostname].nebula_ip}/32"
        for hostname in config.nebula_hostnames
        if mesh.hosts[hostname].role == "non-k8s"
    ]
    if external_ips:
        rules.append(cilium.egress_to_cidrs(*external_ips, ports=[22]))
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
        _bearer_credentials(self)

    def _add_deployment(self, config_map: ConfigMap, *, config: SshMcpConfig, mesh: Mesh) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(NAME, NAMESPACE, labels=LABELS, annotations={"reloader.stakater.com/auto": "true"}),
            pod_metadata=ApiObjectMetadata(labels=LABELS),
            replicas=1,
            strategy=DeploymentStrategy.recreate(),
            select=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            automount_service_account_token=False,
            enable_service_links=False,
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
        )
        # The Deployment selector is immutable; retain its existing labels for Flux adoption.
        deployment.select(LabelSelector.of(labels=LABELS))
        ApiObject.of(deployment).add_json_patch(runtime_default_seccomp_patch())
        bearer = Secret.from_secret_name(self, "bearer-secret-ref", BEARER_SECRET_NAME)
        deployment.add_container(
            name="server",
            image=f"{IMAGE_NAME}:{PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            env_variables={
                "SSH_MCP_CONFIG_FILE": EnvValue.from_value(f"{CONFIG_DIR}/settings.yaml"),
                "SSH_MCP_BEARER_TOKEN": EnvValue.from_secret_value(SecretValue(secret=bearer, key=BEARER_SECRET_KEY)),
                "SSH_MCP_HOST": EnvValue.from_value("0.0.0.0"),
                "SSH_MCP_PORT": EnvValue.from_value(str(HTTP_PORT)),
            },
            ports=[ContainerPort(name="http", number=HTTP_PORT, protocol=Protocol.TCP)],
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(500)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            readiness=http_probe("/healthz", port=HTTP_PORT, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=HTTP_PORT, initial_delay_seconds=15, period_seconds=20),
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
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(NAME, NAMESPACE),
            selector=deployment,
            ports=[ServicePort(name="http", port=HTTP_PORT, target_port=HTTP_PORT, protocol=Protocol.TCP)],
        )

    def _add_network_policy(self, config: SshMcpConfig, mesh: Mesh) -> None:
        cilium.network_policy(
            self,
            "network-policy",
            metadata=metadata(NAME, NAMESPACE),
            selector=LABELS,
            ingress=[
                cilium.ingress_from(
                    cilium.endpoint_labels("agentplane-staging", "agentplane-actions"), ports=[HTTP_PORT]
                )
            ],
            egress=_egress(mesh, config),
        )


def chart(app: App, *, config: SshMcpConfig, mesh: Mesh) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={"name": NAMESPACE, "goldilocks.fairwinds.com/enabled": "false"},
            annotations={"description": "SSH MCP backend; private keys stay in this namespace."},
        ),
    )
    SshMcp(chart, NAME, config=config, mesh=mesh)
    return chart
