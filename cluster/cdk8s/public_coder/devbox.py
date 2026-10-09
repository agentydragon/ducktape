"""The public-coder-agent devbox: its KubeVirt VirtualMachine, SSH Service, Bazel cache claim,
and BuildBuddy API key. Paused alongside OpenClaw; its cache PVC remains declared.

Hand-written beside the generated output: the sshd host key's SOPS Secret, and `image-pins/`,
whose image-automation marker overrides the VM placeholder image tag
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import Pods, Service, ServiceType, k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachineSpecTemplateSpecAffinity,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinity,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesLimits,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesConfigMap,
    VirtualMachineSpecTemplateSpecVolumesPersistentVolumeClaim,
    VirtualMachineSpecTemplateSpecVolumesSecret,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import external_creds, node_scheduling, service_ref
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_app, write_yaml
from cluster.cdk8s.kubevirt.virtual_machine import container_disk_vm, domain_labels
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.providers.kubevirt.virtual_machine import VirtualMachine

NAMESPACE = "public-coder-agent"
VM_NAME = "public-coder-devbox"
# The VM's sshd, which ssh-mcp and sshpiper dial. `service_ref` stays qualified because
# cdk8s-plus's `Pods` is imported bare.
SSH = service_ref.ServiceRef(
    name="public-coder-devbox-ssh",
    port=service_ref.Port(name="ssh", number=22),
    pods=service_ref.Pods(namespace=NAMESPACE, labels=tuple(domain_labels(VM_NAME).items())),
)
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/public-coder-agent/devbox"
_SERVICE_LABELS = {"app.kubernetes.io/name": VM_NAME}
_BAZEL_CACHE_CLAIM = "public-coder-devbox-bazel-cache"
_BUILDBUDDY_API_KEY = "buildbuddy-api-key"
# The tag comes from image-pins/kustomization.yaml.
_IMAGE = "git.allegedly.works/ducktape-ci/public-coder-devbox:unset"


def ssh_service(scope: Construct) -> Service:
    """Create the in-cluster Service that exposes the KubeVirt VM's SSH port."""
    return Service(
        scope,
        "ssh-service",
        metadata=ApiObjectMetadata(
            name=SSH.name,
            namespace=SSH.pods.namespace,
            labels=_SERVICE_LABELS,
            annotations={
                "description": (
                    "ClusterIP for operator port-forward access (e.g. nixos-rebuild switch per "
                    "docs/kubevirt_nixos_vm.md) and for ssh-mcp's public-coder-devbox targets "
                    "(cluster/k8s/ssh-mcp); not public."
                )
            },
        ),
        selector=Pods.select(scope, "devbox-pods", labels=SSH.pods.selector),
        ports=[SSH.port.service_port()],
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
    ExternalSecret(
        scope,
        "buildbuddy-api-key",
        metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data(name, "api-key")],
        # Reuse the existing Reflector mirror during the staged ownership handoff.
        creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )


def virtual_machine(scope: Construct) -> VirtualMachine:
    """The devbox VM, booting the ephemeral containerDisk image with its cache, CA and keys attached."""
    return container_disk_vm(
        scope,
        "virtual-machine",
        name=VM_NAME,
        namespace=NAMESPACE,
        run_strategy="Halted",  # Public Coder is paused; retain the VM definition and cache PVC.
        # This is an ephemeral VM disk (accepted tradeoff: no checkout or other local
        # state survives an image update or VM restart). Flux ImageUpdateAutomation
        # sets the tag in image-pins/ after .github/workflows/public-coder-devbox-image.yml
        # publishes a new one -- see the public-coder-devbox ImagePolicy in
        # forgejo/image_automation.py.
        image=_IMAGE,
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
                                    key=node_scheduling.CONTROL_PLANE_TAINT_KEY, operator="DoesNotExist"
                                )
                            ]
                        )
                    ]
                )
            )
        ),
        ports=[VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(name=SSH.port.name, port=SSH.pod_port)],
        disks={
            "pcbazelcache": VirtualMachineSpecTemplateSpecVolumes(
                name="bazel-cache",
                persistent_volume_claim=VirtualMachineSpecTemplateSpecVolumesPersistentVolumeClaim(
                    claim_name=_BAZEL_CACHE_CLAIM
                ),
            ),
            "pcproxyca": VirtualMachineSpecTemplateSpecVolumes(
                name="proxy-ca",
                config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name="public-coder-agent-proxy-ca-cert"),
            ),
            "pcbuildbuddy": VirtualMachineSpecTemplateSpecVolumes(
                name="buildbuddy-api-key",
                secret=VirtualMachineSpecTemplateSpecVolumesSecret(secret_name=_BUILDBUDDY_API_KEY),
            ),
            "pchostkey": VirtualMachineSpecTemplateSpecVolumes(
                name="ssh-host-key",
                secret=VirtualMachineSpecTemplateSpecVolumesSecret(secret_name="public-coder-devbox-ssh-host-key"),
            ),
        },
    )


def write_manifests(root: Path) -> Service:
    """Write the devbox manifests and return the generated SSH Service."""
    app = App()
    chart = Chart(app, VM_NAME, disable_resource_name_hashes=True)
    service = ssh_service(chart)
    virtual_machine(chart)
    _bazel_cache_claim(chart)
    _buildbuddy_api_key(chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(
            resources=["ssh-host-key.sops.yaml", write_app(root, OUTPUT_DIR, app)], components=["./image-pins"]
        ),
    )
    return service


def public_coder_agent_devbox(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    kubevirt: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    name = "public-coder-agent-devbox"
    return flux_kustomization(
        chart,
        name,
        artifact,
        # A deliberately halted VM is not Ready. Do not block Flux on guest readiness.
        wait=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="30m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(kubevirt, external_secrets_operator),
        description=(
            "Paused Public Coder devbox: VM halted and image restart controller removed; "
            "the separately declared Bazel cache PVC and SSH identity are retained."
        ),
    )
