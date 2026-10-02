"""The NVIDIA DCGM exporter: GPU metrics to Prometheus (`/metrics` on :9400).

The PodMonitor is auto-discovered by Alloy (`prometheus.operator.podmonitors "cluster"`) and
forwarded to Mimir (365 d). The
motivation is getting DCGM_FI_DEV_XID_ERRORS (the `Xid 79 "GPU has fallen off the bus"`
signal), PCIe replay counters, and power/temp into Mimir to characterize the recurring RTX 5090
fall-off events; this replaces the local-CSV nix/nixos/modules/gpu-monitor.nix poller.
Context: debug/atlas/gpu_lockup_20260718_followups.md, cluster/docs/plan.md.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import PodMonitorSpecSelector

from cluster.cdk8s import namespaces
from cluster.cdk8s.dcgm_exporter import counters
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.prometheus_operator.pod_monitor import Endpoint, PodMonitor

NAME = "dcgm-exporter"
NAMESPACE = "dcgm-exporter"
OUTPUT_DIR = f"{GENERATED_ROOT}/dcgm-exporter"
_LABELS = {"app": NAME}
_CONFIG_DIR = "/etc/dcgm-exporter-config"
_COUNTERS_FILE = "counters.csv"
# A configMapGenerator literal: its hash suffix rewrites the DaemonSet's volume reference and
# rolls the pods whenever the counter set changes.
COUNTERS_CONFIG_MAP = ConfigMapArgs(
    name="dcgm-counters", namespace=NAMESPACE, literals=[f"{_COUNTERS_FILE}={counters.render()}"]
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        # Fixed-resource DaemonSet -- opt out of Goldilocks/VPA recommendations.
        vpa=Vpa.DISABLED,
        labels={
            # GPU exporter runs with the nvidia runtimeClass and host GPU device injection.
            "pod-security.kubernetes.io/enforce": "privileged",
            "pod-security.kubernetes.io/audit": "privileged",
            "pod-security.kubernetes.io/warn": "privileged",
        },
    )
    k8s.KubeDaemonSet(
        chart,
        "daemonset",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.DaemonSetSpec(
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    # Only wyrm2 has GPUs. Generalize to a GPU label if more GPU nodes appear.
                    node_selector={"kubernetes.io/hostname": "wyrm2"},
                    # nvidia-container-runtime.cdi injects /dev/nvidia*, driver libs, and NVML.
                    runtime_class_name="nvidia",
                    containers=[
                        k8s.Container(
                            name=NAME,
                            image=(
                                "nvcr.io/nvidia/k8s/dcgm-exporter:4.5.2-4.8.1-ubuntu22.04"
                                "@sha256:17e9d49093f2d86d90260eec3c07b18b3f78fc5eadef3c8ab26a24fa5a9c1b2c"
                            ),
                            # Custom counter set -- the stock default omits DCGM_FI_DEV_XID_ERRORS.
                            args=["-f", f"{_CONFIG_DIR}/{_COUNTERS_FILE}"],
                            env=[
                                # Inject all GPUs via the runtime. Do NOT request nvidia.com/gpu --
                                # a countable GPU request would reserve a device and starve real
                                # workloads; the exporter only needs NVML visibility of every GPU.
                                k8s.EnvVar(name="NVIDIA_VISIBLE_DEVICES", value="all"),
                                # DCGM talks to NVML + libdcgm; "all" guarantees the driver
                                # libraries the runtime injects cover them (superset of "utility").
                                k8s.EnvVar(name="NVIDIA_DRIVER_CAPABILITIES", value="all"),
                            ],
                            ports=[k8s.ContainerPort(name="metrics", container_port=9400)],
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string("metrics")),
                                initial_delay_seconds=10,
                                period_seconds=10,
                            ),
                            volume_mounts=[k8s.VolumeMount(name="counters", mount_path=_CONFIG_DIR, read_only=True)],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("500m"),
                                    "memory": k8s.Quantity.from_string("512Mi"),
                                },
                            ),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="counters", config_map=k8s.ConfigMapVolumeSource(name=COUNTERS_CONFIG_MAP.name))
                    ],
                ),
            ),
        ),
    )
    # Alloy's prometheus.operator.podmonitors "cluster" auto-discovers this cluster-wide and
    # forwards the scraped series to Mimir. No Alloy config change is needed.
    PodMonitor(
        chart,
        "podmonitor",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE, labels=_LABELS),
        selector=PodMonitorSpecSelector(match_labels=_LABELS),
        pod_metrics_endpoints=[Endpoint.plain(port="metrics")],
    )
    return chart


def dcgm_exporter(chart: Chart, directory: RenderedDirectory, monitoring_crds: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="2m",
        # the PodMonitor CRD
        depends_on=flux_kustomization_depends_on_many(monitoring_crds),
    )
