"""The VM-isolated BuildBuddy workers for opt-in KVM tests."""

import yaml
from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu, k8s
from kubevirt_virtualmachinepool_crds.io.kubevirt.pool import (
    VirtualMachinePoolSpecMaxUnavailable,
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
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecReadinessProbe,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecReadinessProbeHttpGet,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecReadinessProbeHttpGetHttpHeaders,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecReadinessProbeHttpGetPort,
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
from cluster.cdk8s.kubevirt.virtual_machine_pool import container_disk_pool_template
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.oci_cache import zot
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.kubevirt.virtual_machine_pool import VirtualMachinePool
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor

NAME = "buildbuddy-kvm-worker"
NAMESPACE = "buildbuddy-executor"
# The executor serves Prometheus metrics here (BuildBuddy's default monitoring port), on the guest.
_METRICS_PORT = 9090
_METRICS_PORT_NAME = "metrics"
_NODE_METRICS_PORT = 9100
_NODE_METRICS_PORT_NAME = "node-metrics"
_METRICS_SERVICE = f"{NAME}-metrics"
# Selects every worker's pods for one Service, whatever their names.
_WORKER_LABELS = {"app.kubernetes.io/part-of": NAME}
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
# Eight GiB suffices for the one test CI gates on this pool. During test_firecracker (commit
# 0b00b93f0bc0a8043fb2afee0b59d653f35812b8, BuildBuddy invocation f6ad2fda-409c-446f-8ade-8ac18f5da377, 2026-11-10)
# guest memory in use, MemTotal less MemAvailable, peaked at 2.8 GiB on both workers, sampled over the VM console
# and not retained; an idle worker uses 0.75 GiB. test_firecracker reserves 6 GiB (its EstimatedMemory),
# and the executor advertises 7 GiB (devinfra/buildbuddy/kvm/worker/executor.yaml), which leaves 1 GiB for the guest.
_MEMORY = Size.gibibytes(8)
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
            "runcmd": [
                ["apt-get", "install", "-y", "--no-install-recommends", "prometheus-node-exporter"],
                ["bash", "/run/buildbuddy-config/bootstrap.sh"],
            ],
            "power_state": {"mode": "reboot", "delay": "now", "condition": ["test", "-f", "/var/run/reboot-required"]},
            "ssh_pwauth": False,
            "disable_root": True,
        },
        sort_keys=False,
    )


def _pool_worker_template() -> VirtualMachinePoolSpecVirtualMachineTemplate:
    """One worker: a VM with a per-VM SSD work disk, in the pool's template types."""
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
                    _MEMORY.as_string()
                ),
            },
            limits={
                "cpu": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResourcesLimits.from_string(
                    Cpu.units(4).amount
                ),
                "memory": VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResourcesLimits.from_string(
                    _MEMORY.as_string()
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
        # Ready once the executor has registered with BuildBuddy, not merely once the VM runs, so a rollout does
        # not restart the next member while the pool has no executor. `/readyz` answers 200 when registered and 503
        # otherwise, on the executor's HTTP port; it rejects a request that does not name the server type.
        readiness_probe=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecReadinessProbe(
            http_get=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecReadinessProbeHttpGet(
                path="/readyz",
                port=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecReadinessProbeHttpGetPort.from_number(
                    8080
                ),
                http_headers=[
                    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecReadinessProbeHttpGetHttpHeaders(
                        name="server-type", value="buildbuddy-executor"
                    )
                ],
            ),
            period_seconds=10,
            failure_threshold=3,
            success_threshold=1,
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
        # CDI creates one claim per VM from this template: a blank disk image on a filesystem volume on the node's
        # SSD pool. Not a raw block volume: CDI's importer runs as a non-root user, which cannot open the device.
        # TODO(https://github.com/agentydragon/ducktape/issues/15297): use raw block once containerd on these nodes
        # sets device_ownership_from_security_context.
        # The contents rebuild, so the class's single unreplicated copy is enough.
        data_volume_templates=[
            VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplates(
                metadata={"name": "work"},
                spec=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpec(
                    source=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecSource(blank={}),
                    storage=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecStorage(
                        access_modes=["ReadWriteOnce"],
                        storage_class_name="piraeus-ssd-local",
                        volume_mode="Filesystem",
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
    # Each worker registers its own executor in pool `kvm-vm` and runs one action at a time.
    VirtualMachinePool(
        chart,
        "pool",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE, labels=_WORKER_LABELS),
        selector=VirtualMachinePoolSpecSelector(match_labels=_WORKER_LABELS),
        virtual_machine_template=_pool_worker_template(),
        replicas=2,
        # A template change replaces VMs one at a time, so the pool is never left without an executor.
        max_unavailable=VirtualMachinePoolSpecMaxUnavailable.from_number(1),
    )
    NetworkPolicy(
        chart,
        "pool-network",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        endpoint_selector=_WORKER_LABELS,
        ingress=[
            IngressRule.from_endpoints(
                cilium.endpoint_labels("monitoring", "alloy-node"), ports=[_METRICS_PORT, _NODE_METRICS_PORT]
            )
        ],
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
                ),
                k8s.ServicePort(
                    name=_NODE_METRICS_PORT_NAME,
                    port=_NODE_METRICS_PORT,
                    target_port=k8s.IntOrString.from_number(_NODE_METRICS_PORT),
                    protocol="TCP",
                ),
            ],
            type="ClusterIP",
        ),
    )
    ServiceMonitor(
        chart,
        "service-monitor",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=_WORKER_LABELS),
        endpoints=[Endpoint.plain(port=_METRICS_PORT_NAME), Endpoint.plain(port=_NODE_METRICS_PORT_NAME)],
    )
    target = f'namespace="{NAMESPACE}",service="{_METRICS_SERVICE}",endpoint="{_METRICS_PORT_NAME}"'
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
                        for_="2m",
                        labels={"severity": "critical"},
                        summary="No KVM worker is ready",
                        description=(
                            "No BuildBuddy KVM worker has been ready and scrapable for 2 minutes, so tests targeting "
                            "pool kvm-vm fail immediately instead of queueing."
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
