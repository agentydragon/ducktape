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
    VirtualMachineSpecTemplateSpecVolumesPersistentVolumeClaim,
    VirtualMachineSpecTemplateSpecVolumesSecret,
)
from kubevirt_virtualmachinepool_crds.io.kubevirt.pool import (
    VirtualMachinePoolSpecSelector,
    VirtualMachinePoolSpecVirtualMachineTemplate,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplates,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpec,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecSource,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecStorage,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecStorageResources,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecStorageResourcesRequests,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinity,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityNodeAffinity,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityPodAntiAffinity,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelectorMatchExpressions,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainCpu,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainCpuFeatures,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResources,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResourcesLimits,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResourcesRequests,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecTolerations,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumes,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesCloudInitNoCloud,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesConfigMap,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesDataVolume,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesSecret,
)
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector
from python.runfiles import Runfiles

from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.kubevirt.virtual_machine import container_disk_vm, domain_labels
from cluster.cdk8s.kubevirt.virtual_machine_pool import container_disk_pool_template
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.oci_cache import zot
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.kubevirt.virtual_machine_pool import VirtualMachinePool
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor

NAME = "buildbuddy-kvm-worker"
# Each worker registers its own executor in the pool and runs one action at a time. The first keeps the
# original name so its VM is not recreated.
WORKER_NAMES = (NAME, f"{NAME}-2")
NAMESPACE = "buildbuddy-executor"
# The executor serves Prometheus metrics here (BuildBuddy's default monitoring port), on the guest.
_METRICS_PORT = 9090
_METRICS_PORT_NAME = "metrics"
_METRICS_SERVICE = f"{NAME}-metrics"
# Selects every worker's pods for one Service, whatever their names.
_WORKER_LABELS = {"app.kubernetes.io/part-of": NAME}
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


def _pool_worker_template() -> VirtualMachinePoolSpecVirtualMachineTemplate:
    """The same worker as the VMs above, over the pool's template types, with a per-VM SSD work disk."""
    return container_disk_pool_template(
        labels=_WORKER_LABELS,
        image=_IMAGE,
        # Only OVH nodes carry the SSD pool sized for this disk.
        node_selector={"topology.kubernetes.io/region": "hil"},
        cpu=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainCpu(
            cores=4,
            model="host-passthrough",
            features=[
                VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainCpuFeatures(
                    name=feature, policy="optional"
                )
                for feature in ("svm", "vmx")
            ],
        ),
        resources=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResources(
            requests={
                "cpu": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResourcesRequests.from_string(
                    Cpu.units(4).amount
                ),
                "memory": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResourcesRequests.from_string(
                    Size.gibibytes(16).as_string()
                ),
            },
            limits={
                "cpu": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResourcesLimits.from_string(
                    Cpu.units(4).amount
                ),
                "memory": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResourcesLimits.from_string(
                    Size.gibibytes(16).as_string()
                ),
            },
        ),
        tolerations=[
            VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecTolerations(
                key=node_scheduling.CONTROL_PLANE_TAINT_KEY, operator="Exists", effect="NoSchedule"
            )
        ],
        affinity=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinity(
            node_affinity=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityNodeAffinity(
                required_during_scheduling_ignored_during_execution=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                    node_selector_terms=[
                        VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                            match_expressions=[
                                VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                    key=f"cpu-feature.node.kubevirt.io/{feature}", operator="In", values=["true"]
                                )
                            ]
                        )
                        for feature in ("svm", "vmx")
                    ]
                )
            ),
            # Keep each worker off any node that already runs another one, whichever kind it is.
            pod_anti_affinity=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityPodAntiAffinity(
                required_during_scheduling_ignored_during_execution=[
                    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                        topology_key="kubernetes.io/hostname",
                        label_selector=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector(
                            match_expressions=[
                                VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelectorMatchExpressions(
                                    key=key, operator="In", values=[value]
                                )
                                for key, value in _WORKER_LABELS.items()
                            ]
                        ),
                    )
                ]
            ),
        ),
        ports=[],
        disks={
            "work": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumes(
                name="work",
                data_volume=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesDataVolume(name="work"),
            ),
            "bbkey": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumes(
                name="buildbuddy-key",
                secret=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesSecret(
                    secret_name="buildbuddy-api-key"
                ),
            ),
            "bbconfig": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumes(
                name="worker-config",
                config_map=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesConfigMap(name=NAME),
            ),
            "cloudinit": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumes(
                name="cloudinit",
                cloud_init_no_cloud=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesCloudInitNoCloud(
                    user_data=_cloud_init()
                ),
            ),
        },
        # CDI creates one claim per VM from this template: a blank block device on the node's SSD pool.
        # The contents rebuild, so the class's single unreplicated copy is enough.
        data_volume_templates=[
            VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplates(
                metadata={"name": "work"},
                spec=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpec(
                    source=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecSource(blank={}),
                    storage=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecStorage(
                        access_modes=["ReadWriteOnce"],
                        storage_class_name="piraeus-ssd-local",
                        volume_mode="Block",
                        resources=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecStorageResources(
                            requests={
                                "storage": VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecStorageResourcesRequests.from_string(
                                    "80Gi"
                                )
                            }
                        ),
                    ),
                ),
            )
        ],
        autoattach_graphics_device=False,
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
    for name in WORKER_NAMES:
        # Heavy scratch (Docker and containerd layers, K3s, action workspaces, cache) is rebuildable, so one
        # unreplicated copy on the node's SSD pool; the claim then keeps this VM on the node that holds it.
        k8s.KubePersistentVolumeClaim(
            chart,
            f"{name}-work-claim",
            metadata=k8s.ObjectMeta(name=f"{name}-work", namespace=NAMESPACE),
            spec=k8s.PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                storage_class_name="piraeus-ssd-local",
                volume_mode="Block",
                resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("80Gi")}),
            ),
        )
        container_disk_vm(
            chart,
            f"{name}-vm",
            name=name,
            namespace=NAMESPACE,
            image=_IMAGE,
            # Only OVH nodes carry the SSD pool sized for this disk.
            node_selector={"topology.kubernetes.io/region": "hil"},
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
                    persistent_volume_claim=VirtualMachineSpecTemplateSpecVolumesPersistentVolumeClaim(
                        claim_name=f"{name}-work"
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
            pod_labels=_WORKER_LABELS,
            annotations={
                "description": (
                    "Static trusted-code BuildBuddy worker. Privileged Docker test actions run inside the guest; "
                    "the guest has no outer Kubernetes token or host filesystem mounts."
                )
            },
        )
        NetworkPolicy(
            chart,
            f"{name}-network",
            metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
            endpoint_selector=domain_labels(name),
            ingress=[
                IngressRule.from_endpoints(cilium.endpoint_labels("monitoring", "alloy-node"), ports=[_METRICS_PORT])
            ],
            egress=[
                cilium.dns_egress(resolves=["*"]),
                EgressRule.to_fqdns(*_HTTPS_HOSTS),
                EgressRule.to_endpoints(cilium.endpoint_labels(zot.HTTP.pods.namespace, "zot"), zot.HTTP.pod_port),
            ],
        )
    # One pool member alongside the VMs above, so capacity never drops while the pool is proven.
    VirtualMachinePool(
        chart,
        "pool",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE, labels=_WORKER_LABELS),
        selector=VirtualMachinePoolSpecSelector(match_labels=_WORKER_LABELS),
        virtual_machine_template=_pool_worker_template(),
        replicas=1,
    )
    NetworkPolicy(
        chart,
        "pool-network",
        # The standalone VMs' per-name policies cover them until they are retired, so this one is named apart.
        metadata=ApiObjectMetadata(name=f"{NAME}-pool", namespace=NAMESPACE),
        endpoint_selector=_WORKER_LABELS,
        ingress=[IngressRule.from_endpoints(cilium.endpoint_labels("monitoring", "alloy-node"), ports=[_METRICS_PORT])],
        egress=[
            cilium.dns_egress(resolves=["*"]),
            EgressRule.to_fqdns(*_HTTPS_HOSTS),
            EgressRule.to_endpoints(cilium.endpoint_labels(zot.HTTP.pods.namespace, "zot"), zot.HTTP.pod_port),
        ],
    )
    k8s.KubeService(
        chart,
        "metrics-service",
        metadata=k8s.ObjectMeta(name=_METRICS_SERVICE, namespace=NAMESPACE, labels=_WORKER_LABELS),
        spec=k8s.ServiceSpec(
            selector=_WORKER_LABELS,
            ports=[
                k8s.ServicePort(
                    name=_METRICS_PORT_NAME,
                    port=_METRICS_PORT,
                    target_port=k8s.IntOrString.from_number(_METRICS_PORT),
                    protocol="TCP",
                )
            ],
            type="ClusterIP",
        ),
    )
    ServiceMonitor(
        chart,
        "service-monitor",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=_WORKER_LABELS),
        endpoints=[Endpoint.plain(port=_METRICS_PORT_NAME)],
    )
    target = f'namespace="{NAMESPACE}",service="{_METRICS_SERVICE}"'
    PrometheusRule(
        chart,
        "prometheus-rule",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        groups=[
            group(
                NAME,
                [
                    Rule.alert(
                        "BuildBuddyKvmPoolEmpty",
                        f"absent(up{{{target}}} == 1)",
                        for_="5m",
                        labels={"severity": "critical"},
                        summary="No KVM worker is reachable",
                        description=(
                            "No BuildBuddy KVM worker has been scrapable for 5 minutes, so tests targeting pool "
                            "kvm-vm fail immediately instead of queueing."
                        ),
                    )
                ],
            )
        ],
    )
    return chart


def buildbuddy_kvm_worker(
    chart: Chart,
    directory: RenderedDirectory,
    executor: Kustomization,
    kubevirt: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="15m",
        suspend=False,
        depends_on=flux_kustomization_depends_on_many(executor, kubevirt, monitoring_crds),
    )
