"""The static, VM-isolated BuildBuddy worker for opt-in KVM tests."""

import yaml
from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainCpuFeatures,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesLimits,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesCloudInitNoCloud,
    VirtualMachineSpecTemplateSpecVolumesConfigMap,
    VirtualMachineSpecTemplateSpecVolumesEmptyDisk,
    VirtualMachineSpecTemplateSpecVolumesEmptyDiskCapacity,
    VirtualMachineSpecTemplateSpecVolumesSecret,
)

from cluster.cdk8s import cilium
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.kubevirt.virtual_machine import container_disk_vm, domain_labels
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.oci_cache import zot
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy, isolate_ingress
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAME = "buildbuddy-kvm-worker"
NAMESPACE = "buildbuddy-executor"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
# KubeVirt's curated Ubuntu 24.04 cloud image, mirrored from Canonical with checksum verification.
_IMAGE = "quay.io/containerdisks/ubuntu:24.04@sha256:ea164c514b308286bdc179b403298efd2d8ae549af518a82a029e1bbb027f3a4"
_HTTPS_HOSTS = ("remote.buildbuddy.io", "buildbuddy.bbcr.io", "archive.ubuntu.com", "security.ubuntu.com")
_GUEST_FILES = (
    "bootstrap.sh",
    "containerd-mounts.conf",
    "containerd.toml",
    "run-executor.sh",
    "executor.yaml",
    "executor.service",
    "daemon.json",
    "modules-load.conf",
    "modprobe.conf",
    "sysctl.conf",
)


def _cloud_init() -> str:
    return "#cloud-config\n" + yaml.safe_dump(
        {
            "apt": {
                "primary": [{"arches": ["amd64"], "uri": "https://archive.ubuntu.com/ubuntu"}],
                "security": [{"arches": ["amd64"], "uri": "https://security.ubuntu.com/ubuntu"}],
            },
            "package_update": True,
            "packages": ["docker.io"],
            "fs_setup": [{"device": "/dev/disk/by-id/virtio-work", "filesystem": "ext4", "overwrite": False}],
            "mounts": [
                ["/dev/disk/by-id/virtio-work", "/buildbuddy", "ext4", "defaults", "0", "2"],
                ["/dev/disk/by-id/virtio-bbkey", "/run/buildbuddy-key", "iso9660", "ro", "0", "0"],
                ["/dev/disk/by-id/virtio-bbconfig", "/run/buildbuddy-config", "iso9660", "ro", "0", "0"],
            ],
            "runcmd": [["bash", "/run/buildbuddy-config/bootstrap.sh"]],
            "power_state": {"mode": "reboot", "delay": "now", "condition": ["test", "-f", "/var/run/reboot-required"]},
            "ssh_pwauth": False,
            "disable_root": True,
        },
        sort_keys=False,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeConfigMap(
        chart,
        "guest-config",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        data={
            name: get_required_path(own_repo_rlocation(f"devinfra/buildbuddy/kvm/worker/{name}")).read_text()
            for name in _GUEST_FILES
        },
    )
    container_disk_vm(
        chart,
        "worker",
        name=NAME,
        namespace=NAMESPACE,
        image=_IMAGE,
        cpu=VirtualMachineSpecTemplateSpecDomainCpu(
            cores=8,
            model="host-passthrough",
            features=[VirtualMachineSpecTemplateSpecDomainCpuFeatures(name="svm", policy="require")],
        ),
        resources=VirtualMachineSpecTemplateSpecDomainResources(
            requests={
                "cpu": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("8"),
                "memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("24Gi"),
            },
            limits={
                "cpu": VirtualMachineSpecTemplateSpecDomainResourcesLimits.from_string("8"),
                "memory": VirtualMachineSpecTemplateSpecDomainResourcesLimits.from_string("24Gi"),
            },
        ),
        node_selector={"kubernetes.io/hostname": "wyrm2"},
        ports=[],
        disks={
            "work": VirtualMachineSpecTemplateSpecVolumes(
                name="work",
                empty_disk=VirtualMachineSpecTemplateSpecVolumesEmptyDisk(
                    capacity=VirtualMachineSpecTemplateSpecVolumesEmptyDiskCapacity.from_string("80Gi")
                ),
            ),
            "bbkey": VirtualMachineSpecTemplateSpecVolumes(
                name="buildbuddy-key",
                secret=VirtualMachineSpecTemplateSpecVolumesSecret(secret_name="buildbuddy-api-key"),
            ),
            "bbconfig": VirtualMachineSpecTemplateSpecVolumes(
                name="worker-config", config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=NAME)
            ),
            "cloudinit": VirtualMachineSpecTemplateSpecVolumes(
                name="cloudinit",
                cloud_init_no_cloud=VirtualMachineSpecTemplateSpecVolumesCloudInitNoCloud(user_data=_cloud_init()),
            ),
        },
        autoattach_graphics_device=False,
        annotations={
            "description": (
                "Static trusted-code BuildBuddy worker. Privileged Docker test actions run inside the guest; "
                "the guest has no outer Kubernetes token or host filesystem mounts."
            )
        },
    )
    NetworkPolicy(
        chart,
        "network",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        endpoint_selector=domain_labels(NAME),
        ingress=isolate_ingress(),
        egress=[
            cilium.dns_egress(resolves=["*"]),
            EgressRule.to_fqdns(*_HTTPS_HOSTS),
            EgressRule.to_endpoints(cilium.endpoint_labels(zot.HTTP.pods.namespace, "zot"), zot.HTTP.pod_port),
        ],
    )
    return chart


def buildbuddy_kvm_worker(
    chart: Chart, directory: RenderedDirectory, executor: Kustomization, kubevirt: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="15m",
        suspend=False,
        depends_on=flux_kustomization_depends_on_many(executor, kubevirt),
    )
