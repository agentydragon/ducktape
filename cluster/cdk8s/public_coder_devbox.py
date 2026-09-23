"""The public-coder-agent namespace (with its default ServiceAccount), and the devbox: its
KubeVirt VirtualMachine, SSH Service, Bazel cache claim and BuildBuddy API key.

Hand-written beside the generated output: the sshd host key's SOPS Secret, and `image-pins/`,
whose image-automation marker overrides the VM's placeholder containerDisk tag
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import Namespace, Pods, Protocol, Service, ServicePort, ServiceType, k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachine,
    VirtualMachineSpec,
    VirtualMachineSpecTemplate,
    VirtualMachineSpecTemplateSpec,
    VirtualMachineSpecTemplateSpecAffinity,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinity,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    VirtualMachineSpecTemplateSpecDomain,
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainDevices,
    VirtualMachineSpecTemplateSpecDomainDevicesDisks,
    VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfaces,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts,
    VirtualMachineSpecTemplateSpecDomainFirmware,
    VirtualMachineSpecTemplateSpecDomainFirmwareBootloader,
    VirtualMachineSpecTemplateSpecDomainFirmwareBootloaderEfi,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesLimits,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecNetworks,
    VirtualMachineSpecTemplateSpecNetworksPod,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesConfigMap,
    VirtualMachineSpecTemplateSpecVolumesContainerDisk,
    VirtualMachineSpecTemplateSpecVolumesPersistentVolumeClaim,
    VirtualMachineSpecTemplateSpecVolumesSecret,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import external_creds
from cluster.cdk8s.external_secrets.external_secret import add_external_secret, remote_data
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.metadata import metadata

NAMESPACE = "public-coder-agent"
NAMESPACE_OUTPUT_DIR = "cluster/k8s/agents/public-coder-agent/namespace"
NAMESPACE_MANIFEST = "public-coder-agent.k8s.yaml"
SERVICE_NAME = "public-coder-devbox-ssh"
VM_NAME = "public-coder-devbox"
SSH_PORT = 22
OUTPUT_DIR = "cluster/k8s/agents/public-coder-agent/devbox"
_SERVICE_LABELS = {"app.kubernetes.io/name": VM_NAME}
_POD_LABELS = {"kubevirt.io/domain": VM_NAME}
_BAZEL_CACHE_CLAIM = "public-coder-devbox-bazel-cache"
_BUILDBUDDY_API_KEY = "buildbuddy-api-key"
# The tag comes from image-pins/kustomization.yaml.
_IMAGE = "git.allegedly.works/ducktape-ci/public-coder-devbox:unset"
_NAMESPACE_LABELS = {
    "goldilocks.fairwinds.com/enabled": "true",
    "goldilocks.fairwinds.com/vpa-update-mode": "auto",
    "name": NAMESPACE,
    "rbac.ducktape.io/agent-readable-metadata": "true",
}
_NAMESPACE_ANNOTATIONS = {
    "description": (
        "Second OpenClaw agent, egress-confined to a CONNECT proxy and reachable only through the Authentik proxy "
        "outpost. Opens pull requests against public repositories as agentydragon-agent."
    )
}
_NAMESPACE_RESOURCES = [NAMESPACE_MANIFEST]
_DEVBOX_RESOURCES = ["ssh-host-key.sops.yaml", "public-coder-devbox.k8s.yaml"]


def namespace(scope: Construct) -> Namespace:
    """Create the namespace shared by the public-coder-agent components."""
    return Namespace(
        scope,
        "namespace",
        metadata=ApiObjectMetadata(name=NAMESPACE, labels=_NAMESPACE_LABELS, annotations=_NAMESPACE_ANNOTATIONS),
    )


def default_service_account(scope: Construct, namespace: Namespace) -> None:
    """Give the namespace's `default` ServiceAccount the pull secret for ducktape-ci images.

    ducktape-ci is a private tenant in the in-cluster Forgejo registry; cluster/k8s/forgejo-images
    reflects `forgejo-images-creds` into this namespace. Workloads that don't set their own
    imagePullSecrets (the devbox VM's containerDisk pull) need it here.
    """
    k8s.KubeServiceAccount(
        scope,
        "default-service-account",
        metadata=k8s.ObjectMeta(name="default", namespace=namespace.name),
        image_pull_secrets=[k8s.LocalObjectReference(name="forgejo-images-creds")],
    )


def ssh_service(scope: Construct) -> Service:
    """Create the in-cluster Service that exposes the KubeVirt VM's SSH port."""
    return Service(
        scope,
        "ssh-service",
        metadata=metadata(
            SERVICE_NAME,
            NAMESPACE,
            labels=_SERVICE_LABELS,
            annotations={
                "description": (
                    "ClusterIP for operator port-forward access (e.g. nixos-rebuild switch per "
                    "docs/kubevirt_nixos_vm.md) and for ssh-mcp's public-coder-devbox targets "
                    "(cluster/k8s/ssh-mcp); not public."
                )
            },
        ),
        selector=Pods.select(scope, "devbox-pods", labels=_POD_LABELS),
        ports=[ServicePort(name="ssh", port=SSH_PORT, target_port=SSH_PORT, protocol=Protocol.TCP)],
        type=ServiceType.CLUSTER_IP,
    )


def _bazel_cache_claim(scope: Construct) -> None:
    k8s.KubePersistentVolumeClaim(
        scope,
        "bazel-cache",
        metadata=k8s.ObjectMeta(
            name=_BAZEL_CACHE_CLAIM,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Separately deletable local Bazel output, repository, and action cache for "
                    "public-coder-devbox. Stop the VM before deleting this claim to reset it."
                )
            },
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="lvm-proxmox-hdd-block",
            # The local HDD thin pool has ample capacity for this separately resettable cache.
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("20Gi")}),
            volume_mode="Block",
        ),
    )


def _buildbuddy_api_key(scope: Construct) -> None:
    name = _BUILDBUDDY_API_KEY
    add_external_secret(
        scope,
        "buildbuddy-api-key",
        name=name,
        namespace=NAMESPACE,
        refresh="1h",
        store=external_creds.STORE,
        data=[remote_data(name, "api-key")],
        # Reuse the existing Reflector mirror during the staged ownership handoff.
        creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )


def _disk(
    name: str, serial: str | None = None, boot_order: int | None = None
) -> VirtualMachineSpecTemplateSpecDomainDevicesDisks:
    return VirtualMachineSpecTemplateSpecDomainDevicesDisks(
        name=name,
        boot_order=boot_order,
        serial=serial,
        disk=VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk(bus="virtio"),
    )


def virtual_machine(scope: Construct) -> VirtualMachine:
    """The devbox VM, booting the ephemeral containerDisk image with its cache, CA and keys attached."""
    return VirtualMachine(
        scope,
        "virtual-machine",
        metadata=metadata(VM_NAME, NAMESPACE, labels=_SERVICE_LABELS),
        spec=VirtualMachineSpec(
            run_strategy="Always",
            template=VirtualMachineSpecTemplate(
                metadata=k8s.ObjectMeta(labels=_POD_LABELS | _SERVICE_LABELS),
                spec=VirtualMachineSpecTemplateSpec(
                    node_selector={
                        # The local LVM cache PVC is available only on Proxmox nodes. Its
                        # WaitForFirstConsumer binding then keeps this VM colocated with it.
                        "topology.kubernetes.io/region": "proxmox",
                        "kubevirt.io/schedulable": "true",
                    },
                    affinity=VirtualMachineSpecTemplateSpecAffinity(
                        node_affinity=VirtualMachineSpecTemplateSpecAffinityNodeAffinity(
                            required_during_scheduling_ignored_during_execution=VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                                node_selector_terms=[
                                    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                        match_expressions=[
                                            VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                                key="node-role.kubernetes.io/control-plane", operator="DoesNotExist"
                                            )
                                        ]
                                    )
                                ]
                            )
                        )
                    ),
                    domain=VirtualMachineSpecTemplateSpecDomain(
                        # 4 cores / 8Gi limit, 2 cores / 4Gi request: the two hil, kubevirt.io/schedulable=true
                        # nodes (ovh-ns102453, ovh-ns103711) are 8-core/32Gi OVH KS-5 boxes already carrying the
                        # rest of the cluster -- the original 8-core/8Gi *request* never fit either node's free
                        # capacity (one short on CPU, the other on memory), which is why this VM's virt-launcher
                        # pod sat Pending/Unschedulable. Heavy compute already runs on BuildBuddy RBE, not this
                        # box, so it doesn't need to request that much locally.
                        cpu=VirtualMachineSpecTemplateSpecDomainCpu(cores=4),
                        resources=VirtualMachineSpecTemplateSpecDomainResources(
                            limits={
                                "cpu": VirtualMachineSpecTemplateSpecDomainResourcesLimits.from_string("4"),
                                "memory": VirtualMachineSpecTemplateSpecDomainResourcesLimits.from_string("8Gi"),
                            },
                            requests={
                                "cpu": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("2"),
                                "memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("4Gi"),
                            },
                        ),
                        firmware=VirtualMachineSpecTemplateSpecDomainFirmware(
                            bootloader=VirtualMachineSpecTemplateSpecDomainFirmwareBootloader(
                                efi=VirtualMachineSpecTemplateSpecDomainFirmwareBootloaderEfi(secure_boot=False)
                            )
                        ),
                        devices=VirtualMachineSpecTemplateSpecDomainDevices(
                            disks=[
                                _disk("rootdisk", boot_order=1),
                                _disk("bazel-cache", serial="pcbazelcache"),
                                _disk("proxy-ca", serial="pcproxyca"),
                                _disk("buildbuddy-api-key", serial="pcbuildbuddy"),
                                _disk("ssh-host-key", serial="pchostkey"),
                            ],
                            interfaces=[
                                VirtualMachineSpecTemplateSpecDomainDevicesInterfaces(
                                    name="default",
                                    masquerade={},
                                    ports=[
                                        VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(
                                            name="ssh", port=SSH_PORT
                                        )
                                    ],
                                )
                            ],
                        ),
                    ),
                    networks=[
                        VirtualMachineSpecTemplateSpecNetworks(
                            name="default", pod=VirtualMachineSpecTemplateSpecNetworksPod()
                        )
                    ],
                    volumes=[
                        VirtualMachineSpecTemplateSpecVolumes(
                            name="rootdisk",
                            # This is an ephemeral VM disk (accepted tradeoff: no checkout or other local
                            # state survives an image update or VM restart). Flux ImageUpdateAutomation
                            # sets the tag in image-pins/ after .github/workflows/public-coder-devbox-image.yml
                            # publishes a new one -- see the public-coder-devbox ImagePolicy in
                            # forgejo_image_automation.py.
                            container_disk=VirtualMachineSpecTemplateSpecVolumesContainerDisk(
                                image=_IMAGE, image_pull_policy="IfNotPresent"
                            ),
                        ),
                        VirtualMachineSpecTemplateSpecVolumes(
                            name="bazel-cache",
                            persistent_volume_claim=VirtualMachineSpecTemplateSpecVolumesPersistentVolumeClaim(
                                claim_name=_BAZEL_CACHE_CLAIM
                            ),
                        ),
                        VirtualMachineSpecTemplateSpecVolumes(
                            name="proxy-ca",
                            config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(
                                name="public-coder-agent-proxy-ca-cert"
                            ),
                        ),
                        VirtualMachineSpecTemplateSpecVolumes(
                            name="buildbuddy-api-key",
                            secret=VirtualMachineSpecTemplateSpecVolumesSecret(secret_name=_BUILDBUDDY_API_KEY),
                        ),
                        VirtualMachineSpecTemplateSpecVolumes(
                            name="ssh-host-key",
                            secret=VirtualMachineSpecTemplateSpecVolumesSecret(
                                secret_name="public-coder-devbox-ssh-host-key"
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )


def write_manifests(root: Path) -> Service:
    """Write the namespace and devbox manifests and return the generated SSH Service."""
    namespace_dir = root / NAMESPACE_OUTPUT_DIR
    namespace_dir.mkdir(parents=True, exist_ok=True)
    namespace_app = App(outdir=str(namespace_dir))
    namespace_chart = Chart(namespace_app, NAMESPACE, disable_resource_name_hashes=True)
    default_service_account(namespace_chart, namespace(namespace_chart))
    namespace_app.synth()
    write_yaml(namespace_dir / "kustomization.yaml", kustomize_kustomization(resources=_NAMESPACE_RESOURCES))

    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = Chart(app, VM_NAME, disable_resource_name_hashes=True)
    service = ssh_service(chart)
    virtual_machine(chart)
    _bazel_cache_claim(chart)
    _buildbuddy_api_key(chart)
    app.synth()
    write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(resources=_DEVBOX_RESOURCES, components=["./image-pins"]),
    )
    return service


def public_coder_agent_devbox(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    kubevirt: Kustomization,
    forgejo_images: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
    agent_shared_secrets: Kustomization,
    public_coder_agent_app_kustomization: Kustomization,
) -> Kustomization:
    name = "public-coder-agent-devbox"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="30m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            kubevirt,
            forgejo_images,
            external_creds,
            external_secrets_config,
            agent_shared_secrets,
            public_coder_agent_app_kustomization,
        ),
        description=(
            "KubeVirt build/test devbox for public-coder-agent "
            "(Bazel/BuildBuddy/direnv), with an ephemeral containerDisk root "
            "apart from the sshd host key and SSH access through ../sshpiper."
        ),
    )
