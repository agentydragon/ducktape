"""The static, VM-isolated BuildBuddy worker for opt-in KVM tests."""

import yaml
from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu, k8s
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachineSpecTemplateSpecAffinity,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinity,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    VirtualMachineSpecTemplateSpecAffinityPodAntiAffinity,
    VirtualMachineSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    VirtualMachineSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector,
    VirtualMachineSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelectorMatchExpressions,
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainCpuFeatures,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesLimits,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecTolerations,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesCloudInitNoCloud,
    VirtualMachineSpecTemplateSpecVolumesConfigMap,
    VirtualMachineSpecTemplateSpecVolumesEmptyDisk,
    VirtualMachineSpecTemplateSpecVolumesEmptyDiskCapacity,
    VirtualMachineSpecTemplateSpecVolumesSecret,
)
from python.runfiles import Runfiles

from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.kubevirt.virtual_machine import container_disk_vm, domain_labels
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.oci_cache import zot
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy, isolate_ingress

NAME = "buildbuddy-kvm-worker"
# Each worker registers its own executor in the pool and runs one action at a time. The first keeps the
# original name so its VM is not recreated.
WORKER_NAMES = (NAME, f"{NAME}-2")
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
    "docker-seccomp.json",
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


def _other_workers_anti_affinity(name: str) -> VirtualMachineSpecTemplateSpecAffinityPodAntiAffinity:
    """Keep a worker off any node that already runs another worker, without naming a node."""
    return VirtualMachineSpecTemplateSpecAffinityPodAntiAffinity(
        required_during_scheduling_ignored_during_execution=[
            VirtualMachineSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                topology_key="kubernetes.io/hostname",
                label_selector=VirtualMachineSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector(
                    match_expressions=[
                        VirtualMachineSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelectorMatchExpressions(
                            key="kubevirt.io/domain",
                            operator="In",
                            values=[other for other in WORKER_NAMES if other != name],
                        )
                    ]
                ),
            )
        ]
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    runfiles_root = Runfiles.CreateOrRaise().root()
    k8s.KubeConfigMap(
        chart,
        "guest-config",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        data={
            **{
                name: (runfiles_root / f"ducktape/devinfra/buildbuddy/kvm/worker/{name}").read_text()
                for name in _GUEST_FILES
            },
            "k3s.yaml": yaml_config({"disable-default-registry-endpoint": True}),
            "registries.yaml": yaml_config(
                {
                    "mirrors": {
                        registry: {"endpoint": [f"http://{zot.HTTP.fqdn}"], "rewrite": {"^(.*)$": f"{prefix}/$1"}}
                        for registry, prefix in (
                            ("docker.io", "docker-hub"),
                            ("ghcr.io", "ghcr"),
                            ("quay.io", "quay"),
                            ("registry.k8s.io", "k8s"),
                            ("gcr.io", "gcr"),
                        )
                    }
                }
            ),
        },
    )
    for index, name in enumerate(WORKER_NAMES):
        container_disk_vm(
            chart,
            "worker" if index == 0 else f"worker-{index + 1}",
            name=name,
            namespace=NAMESPACE,
            image=_IMAGE,
            cpu=VirtualMachineSpecTemplateSpecDomainCpu(
                cores=4,
                model="host-passthrough",
                features=[
                    VirtualMachineSpecTemplateSpecDomainCpuFeatures(name=feature, policy="optional")
                    for feature in ("svm", "vmx")
                ],
            ),
            resources=VirtualMachineSpecTemplateSpecDomainResources(
                requests={
                    "cpu": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string(Cpu.units(4).amount),
                    "memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string(
                        Size.gibibytes(16).as_string()
                    ),
                },
                limits={
                    "cpu": VirtualMachineSpecTemplateSpecDomainResourcesLimits.from_string(Cpu.units(4).amount),
                    "memory": VirtualMachineSpecTemplateSpecDomainResourcesLimits.from_string(
                        Size.gibibytes(16).as_string()
                    ),
                },
            ),
            tolerations=[
                VirtualMachineSpecTemplateSpecTolerations(
                    key=node_scheduling.CONTROL_PLANE_TAINT_KEY, operator="Exists", effect="NoSchedule"
                )
            ],
            affinity=VirtualMachineSpecTemplateSpecAffinity(
                pod_anti_affinity=_other_workers_anti_affinity(name),
                node_affinity=VirtualMachineSpecTemplateSpecAffinityNodeAffinity(
                    required_during_scheduling_ignored_during_execution=VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                        node_selector_terms=[
                            VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                match_expressions=[
                                    VirtualMachineSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                        key=f"cpu-feature.node.kubevirt.io/{feature}", operator="In", values=["true"]
                                    )
                                ]
                            )
                            for feature in ("svm", "vmx")
                        ]
                    )
                ),
            ),
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
            "network" if index == 0 else f"network-{index + 1}",
            metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
            endpoint_selector=domain_labels(name),
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
