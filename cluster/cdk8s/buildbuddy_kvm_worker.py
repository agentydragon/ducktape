"""The VM-isolated BuildBuddy workers for opt-in KVM tests."""

import yaml
from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu, k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecIgnore, KustomizationSpecIgnoreTarget
from keda_scaledobject_crds.sh import keda
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

from cluster.cdk8s import cilium, node_scheduling, piraeus
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.kubevirt.virtual_machine_pool import container_disk_pool_template
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.monitoring import mimir
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
# Leave time after BuildBuddy's 24h action timeout for guest services and poweroff.
_VM_SHUTDOWN_GRACE_SECONDS = 87_000
# Selects every worker's pods for one Service, whatever their names.
_WORKER_LABELS = {"app.kubernetes.io/part-of": NAME}
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
AUTOSCALER_NAME = f"{NAME}-autoscaler"
AUTOSCALER_OUTPUT_DIR = f"{GENERATED_ROOT}/{AUTOSCALER_NAME}"

# One task can have reservations on several executors. This is a demand proxy,
# not a count of distinct queued tasks. The assigned CPU gauge is present even
# before the first task; the queue gauge is created only after a group queues.
_METRICS_TARGET = f'namespace="{NAMESPACE}",service="{_METRICS_SERVICE}",endpoint="{_METRICS_PORT_NAME}"'
_WORKER_PODS = (
    f'max by (namespace, pod) (kube_pod_info{{namespace="{NAMESPACE}",'
    f'created_by_kind="VirtualMachineInstance",created_by_name=~"{NAME}-[0-9]+"}})'
)
_UP = f"up{{{_METRICS_TARGET}}}"
_REGISTERED = f'buildbuddy_health_check_status{{{_METRICS_TARGET},health_check_name="registered_to_scheduler"}}'
_HEALTHY_WORKERS = (
    f"({_UP} == 1) and (time() - timestamp({_UP}) < 180) "
    f"and on (namespace, pod) ({_REGISTERED} == 1) "
    f"and on (namespace, pod) (time() - timestamp({_REGISTERED}) < 180) "
    f"and on (namespace, pod) ({_WORKER_PODS})"
)
_ASSIGNED_CPU_METRIC = f"buildbuddy_remote_execution_assigned_milli_cpu{{{_METRICS_TARGET}}}"
_ASSIGNED_CPU = (
    f"{_ASSIGNED_CPU_METRIC} and (time() - timestamp({_ASSIGNED_CPU_METRIC}) < 180)"
    f" and on (namespace, pod) ({_HEALTHY_WORKERS})"
)
_QUEUE = f"buildbuddy_remote_execution_queue_length{{{_METRICS_TARGET}}} and on (namespace, pod) ({_HEALTHY_WORKERS})"
_POOL_REPLICAS = f'kube_customresource_kubevirt_vmpool_desired_replicas{{namespace="{NAMESPACE}",name="{NAME}"}}'
_DESIRED_REPLICAS = f"max({_POOL_REPLICAS} and (time() - timestamp({_POOL_REPLICAS}) < 180))"
_REPORTING_WORKERS = f"(count(({_ASSIGNED_CPU})) or vector(0))"
# Hold at the desired size while a requested VM is still booting or its scrape
# is missing. Missing/stale pool state returns an empty vector, so KEDA holds.
_DEMAND_QUERY = (
    f"((sum(clamp_max(({_ASSIGNED_CPU}), 1)) + "
    f"(sum(({_QUEUE})) or 0 * sum(({_ASSIGNED_CPU})))) "
    f"and on () ({_REPORTING_WORKERS} == {_DESIRED_REPLICAS})) or ({_DESIRED_REPLICAS})"
)
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
_JOURNALD_DROPIN = """# KubeVirt's guest-console-log collector forwards ttyS0 output to Kubernetes logs.
# Do not type credentials into the root-autologin serial console; echoed input and
# journal messages are included in that existing stream.
[Journal]
ForwardToConsole=yes
TTYPath=/dev/ttyS0
MaxLevelConsole=info
SystemMaxUse=32M
SystemKeepFree=192M
RuntimeMaxUse=16M
"""


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
            "write_files": [
                {
                    "path": "/etc/systemd/journald.conf.d/90-buildbuddy-kvm-worker.conf",
                    "permissions": "0644",
                    "content": _JOURNALD_DROPIN,
                }
            ],
            "runcmd": [
                ["systemctl", "restart", "systemd-journald.service"],
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
        termination_grace_period_seconds=_VM_SHUTDOWN_GRACE_SECONDS,
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
        # The contents rebuild, so a single local copy is enough. Deleting the
        # member's PVC also deletes its LINSTOR volume and frees the SSD space.
        data_volume_templates=[
            VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplates(
                metadata={"name": "work"},
                spec=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpec(
                    source=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecSource(blank={}),
                    storage=VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplatesSpecStorage(
                        access_modes=["ReadWriteOnce"],
                        storage_class_name=piraeus.SSD_LOCAL_DELETE,
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
        # The pool controller defaults to one, but the /scale subresource fails
        # unless the field exists. Flux ignores subsequent HPA updates to it.
        replicas=1,
        # A template change replaces at most one VM at a time.
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
                    ),
                    Rule.alert(
                        "BuildBuddyKvmAutoscalerMetricsMissing",
                        f"absent({_DEMAND_QUERY})",
                        for_="5m",
                        labels={"severity": "warning"},
                        summary="KVM worker autoscaler lacks pool metrics",
                        description="KEDA holds the current VM count until fresh pool metrics are available.",
                    ),
                    Rule.alert(
                        "BuildBuddyKvmWorkerNotReporting",
                        f"{_REPORTING_WORKERS} < {_DESIRED_REPLICAS}",
                        for_="15m",
                        labels={"severity": "warning"},
                        summary="KVM worker has not registered or is not scrapable",
                        description="The autoscaler holds its current size until every requested worker reports metrics.",
                    ),
                ],
            )
        ],
    )
    return chart


def autoscaler_chart(app: App) -> Chart:
    chart = Chart(app, AUTOSCALER_NAME, disable_resource_name_hashes=True)
    keda.ScaledObject(
        chart,
        "scaled-object",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        spec=keda.ScaledObjectSpec(
            scale_target_ref=keda.ScaledObjectSpecScaleTargetRef(
                api_version=VirtualMachinePool.GVK.api_version, kind=VirtualMachinePool.GVK.kind, name=NAME
            ),
            min_replica_count=1,
            max_replica_count=3,
            polling_interval=30,
            fallback=keda.ScaledObjectSpecFallback(
                failure_threshold=3,
                replicas=1,
                behavior=keda.ScaledObjectSpecFallbackBehavior.CURRENT_REPLICAS_IF_HIGHER,
            ),
            advanced=keda.ScaledObjectSpecAdvanced(
                horizontal_pod_autoscaler_config=keda.ScaledObjectSpecAdvancedHorizontalPodAutoscalerConfig(
                    behavior=keda.ScaledObjectSpecAdvancedHorizontalPodAutoscalerConfigBehavior(
                        scale_up=keda.ScaledObjectSpecAdvancedHorizontalPodAutoscalerConfigBehaviorScaleUp(
                            stabilization_window_seconds=120,
                            policies=[
                                keda.ScaledObjectSpecAdvancedHorizontalPodAutoscalerConfigBehaviorScaleUpPolicies(
                                    type="Pods", value=1, period_seconds=900
                                )
                            ],
                        ),
                        scale_down=keda.ScaledObjectSpecAdvancedHorizontalPodAutoscalerConfigBehaviorScaleDown(
                            stabilization_window_seconds=900,
                            policies=[
                                keda.ScaledObjectSpecAdvancedHorizontalPodAutoscalerConfigBehaviorScaleDownPolicies(
                                    type="Pods", value=1, period_seconds=300
                                )
                            ],
                        ),
                    )
                )
            ),
            triggers=[
                keda.ScaledObjectSpecTriggers(
                    type="prometheus",
                    metric_type="AverageValue",
                    metadata={
                        "serverAddress": f"{mimir.GATEWAY_URL}/prometheus",
                        "query": _DEMAND_QUERY,
                        "threshold": "1",
                        "ignoreNullValues": "false",
                    },
                )
            ],
        ),
    )
    return chart


def buildbuddy_kvm_worker(
    chart: Chart,
    directory: RenderedDirectory,
    executor: Kustomization,
    kubevirt: Kustomization,
    monitoring_crds: Kustomization,
    piraeus: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="15m",
        suspend=False,
        ignore=[
            KustomizationSpecIgnore(
                paths=["/spec/replicas"],
                target=KustomizationSpecIgnoreTarget(
                    group="pool.kubevirt.io",
                    version="v1beta1",
                    kind="VirtualMachinePool",
                    name=NAME,
                    namespace=NAMESPACE,
                ),
            )
        ],
        depends_on=flux_kustomization_depends_on_many(executor, kubevirt, monitoring_crds, piraeus),
    )


def autoscaler(
    chart: Chart,
    directory: RenderedDirectory,
    worker: Kustomization,
    keda_operator: Kustomization,
    monitoring: Kustomization,
    mimir_backend: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        AUTOSCALER_NAME,
        directory,
        depends_on=flux_kustomization_depends_on_many(worker, keda_operator, monitoring, mimir_backend),
    )
