"""Per-node LVM thin-pool metrics for the explicitly selected Piraeus pool."""

from __future__ import annotations

import re

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ConfigMap, k8s
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from util.bazel.runfiles import get_required_path, own_repo_rlocation

_NAME = "piraeus-lvm-metrics"
_METRICS_PORT_NAME = "metrics"
_METRICS_PORT = 9100
_TEXTFILE_PATH = "/textfiles/lvm_thin_pool.prom"
_COLLECTOR_SCRIPT = get_required_path(own_repo_rlocation("cluster/cdk8s/piraeus-lvm-metrics.sh")).read_text()


def _validate_lvm_name(name: str, *, field: str) -> None:
    # These names are embedded in the shell's Prometheus labels and a fixed `VG/LV` argument.
    if not re.fullmatch(r"[A-Za-z0-9+_.-]+", name):
        raise ValueError(f"{field} must be a non-empty LVM name containing only letters, digits, +, _, ., or -")


def add_thin_pool_monitoring(
    scope: Construct,
    *,
    namespace: str,
    node_selector: dict[str, str],
    volume_group: str,
    thin_pool: str,
    storage_pool: str,
    name: str = _NAME,
) -> None:
    """Build a scoped reader/exporter pair and its Alloy scrape and alert resources.

    The privileged container only runs reporting `lvs` against the supplied thin pool. The
    exporter runs unprivileged and serves the textfile metrics on its Pod IP, where only the
    node-local Alloy DaemonSet is allowed to connect.
    """
    _validate_lvm_name(volume_group, field="volume_group")
    _validate_lvm_name(thin_pool, field="thin_pool")
    _validate_lvm_name(storage_pool, field="storage_pool")
    if not namespace:
        raise ValueError("namespace must be non-empty")
    if not node_selector:
        raise ValueError("node_selector must be non-empty to keep host LVM access node-scoped")

    pod_labels = {"app.kubernetes.io/name": name}
    config_map_name = f"{name}-collector"
    ConfigMap(
        scope,
        "collector-script",
        metadata=ApiObjectMetadata(
            name=config_map_name,
            namespace=namespace,
            annotations={"description": "Reporting-only lvs collector for the selected Piraeus thin pool."},
        ),
        data={"collect.sh": _COLLECTOR_SCRIPT},
    )

    k8s.KubeDaemonSet(
        scope,
        "daemonset",
        metadata=k8s.ObjectMeta(name=name, namespace=namespace, labels=pod_labels),
        spec=k8s.DaemonSetSpec(
            selector=k8s.LabelSelector(match_labels=pod_labels),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=pod_labels),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    host_network=False,
                    node_selector=node_selector,
                    tolerations=[node_scheduling.CONTROL_PLANE_TOLERATION, node_scheduling.ROAMING_TOLERATION],
                    termination_grace_period_seconds=5,
                    volumes=[
                        k8s.Volume(name="host-dev", host_path=k8s.HostPathVolumeSource(path="/dev", type="Directory")),
                        k8s.Volume(name="textfiles", empty_dir=k8s.EmptyDirVolumeSource()),
                        k8s.Volume(
                            name="collector-script",
                            config_map=k8s.ConfigMapVolumeSource(name=config_map_name, default_mode=0o555),
                        ),
                    ],
                    containers=[
                        k8s.Container(
                            name="lvm-collector",
                            image="quay.io/piraeusdatastore/piraeus-server:v1.35.2",
                            image_pull_policy="IfNotPresent",
                            command=["/bin/sh", "/collector/collect.sh"],
                            env=[
                                k8s.EnvVar(
                                    name="LVM_NODE",
                                    value_from=k8s.EnvVarSource(
                                        field_ref=k8s.ObjectFieldSelector(field_path="spec.nodeName")
                                    ),
                                ),
                                k8s.EnvVar(name="LVM_STORAGE_POOL", value=storage_pool),
                                k8s.EnvVar(name="LVM_VOLUME_GROUP", value=volume_group),
                                k8s.EnvVar(name="LVM_THIN_POOL", value=thin_pool),
                            ],
                            security_context=k8s.SecurityContext(
                                privileged=True,
                                run_as_user=0,
                                run_as_group=0,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="host-dev", mount_path="/dev", read_only=True),
                                k8s.VolumeMount(name="textfiles", mount_path="/textfiles"),
                                k8s.VolumeMount(name="collector-script", mount_path="/collector", read_only=True),
                            ],
                        ),
                        k8s.Container(
                            name="node-exporter",
                            image="quay.io/prometheus/node-exporter:v1.12.1",
                            image_pull_policy="IfNotPresent",
                            args=[
                                "--collector.disable-defaults",
                                "--collector.textfile",
                                "--collector.textfile.directory=/textfiles",
                                "--web.listen-address=:9100",
                            ],
                            ports=[
                                k8s.ContainerPort(name=_METRICS_PORT_NAME, container_port=_METRICS_PORT, protocol="TCP")
                            ],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                read_only_root_filesystem=True,
                                run_as_non_root=True,
                                run_as_user=65534,
                                run_as_group=65534,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                            ),
                            volume_mounts=[k8s.VolumeMount(name="textfiles", mount_path="/textfiles", read_only=True)],
                        ),
                    ],
                ),
            ),
        ),
    )

    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=name, namespace=namespace, labels=pod_labels),
        spec=k8s.ServiceSpec(
            selector=pod_labels,
            ports=[
                k8s.ServicePort(
                    name=_METRICS_PORT_NAME,
                    port=_METRICS_PORT,
                    target_port=k8s.IntOrString.from_string(_METRICS_PORT_NAME),
                    protocol="TCP",
                )
            ],
            type="ClusterIP",
        ),
    )

    NetworkPolicy(
        scope,
        "network-policy",
        metadata=ApiObjectMetadata(
            name=name,
            namespace=namespace,
            annotations={"description": "Allow only node-local Alloy to scrape the LVM textfile exporter."},
        ),
        endpoint_selector=pod_labels,
        ingress=[IngressRule.from_endpoints(cilium.endpoint_labels("monitoring", "alloy-node"), ports=[_METRICS_PORT])],
    )

    ServiceMonitor(
        scope,
        "service-monitor",
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        selector=ServiceMonitorSpecSelector(match_labels=pod_labels),
        endpoints=[Endpoint.plain(port=_METRICS_PORT_NAME)],
    )

    labels = f'namespace="{namespace}",service="{name}",vg="{volume_group}",pool="{thin_pool}"'
    target = f'namespace="{namespace}",service="{name}"'
    expected = f'kube_daemonset_status_desired_number_scheduled{{namespace="{namespace}",daemonset="{name}"}} > 0'
    PrometheusRule(
        scope,
        "prometheus-rule",
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        groups=[
            group(
                name,
                [
                    Rule.alert(
                        "PiraeusThinPoolDataUsageWarning",
                        f"(lvm_thin_pool_data_percent{{{labels}}} >= 80) and (lvm_thin_pool_data_percent{{{labels}}} < 90)",
                        for_="2m",
                        labels={"severity": "warning"},
                        summary="Piraeus thin-pool data usage is high",
                        description=f"Thin-pool {volume_group}/{thin_pool} data usage has been at least 80% for 2 minutes.",
                    ),
                    Rule.alert(
                        "PiraeusThinPoolDataUsageCritical",
                        f"lvm_thin_pool_data_percent{{{labels}}} >= 90",
                        for_="2m",
                        labels={"severity": "critical"},
                        summary="Piraeus thin-pool data usage is critical",
                        description=f"Thin-pool {volume_group}/{thin_pool} data usage has been at least 90% for 2 minutes.",
                    ),
                    Rule.alert(
                        "PiraeusThinPoolMetadataUsageWarning",
                        f"(lvm_thin_pool_metadata_percent{{{labels}}} >= 70) and (lvm_thin_pool_metadata_percent{{{labels}}} < 85)",
                        for_="2m",
                        labels={"severity": "warning"},
                        summary="Piraeus thin-pool metadata usage is high",
                        description=f"Thin-pool {volume_group}/{thin_pool} metadata usage has been at least 70% for 2 minutes.",
                    ),
                    Rule.alert(
                        "PiraeusThinPoolMetadataUsageCritical",
                        f"lvm_thin_pool_metadata_percent{{{labels}}} >= 85",
                        for_="2m",
                        labels={"severity": "critical"},
                        summary="Piraeus thin-pool metadata usage is critical",
                        description=f"Thin-pool {volume_group}/{thin_pool} metadata usage has been at least 85% for 2 minutes.",
                    ),
                    Rule.alert(
                        "PiraeusThinPoolCollectorFailed",
                        f"lvm_thin_pool_collector_success{{{labels}}} == 0",
                        for_="2m",
                        labels={"severity": "warning"},
                        summary="Piraeus thin-pool metrics collection is failing",
                        description=f"Reporting lvs has not returned both usage fields for {volume_group}/{thin_pool} for 2 minutes.",
                    ),
                    Rule.alert(
                        "PiraeusThinPoolCollectorStale",
                        "("
                        + " or ".join(
                            [
                                f"time() - lvm_thin_pool_collector_last_success_timestamp_seconds{{{labels}}} > 90",
                                f"absent(lvm_thin_pool_collector_last_success_timestamp_seconds{{{labels}}})",
                                f'time() - node_textfile_mtime_seconds{{{target},file="{_TEXTFILE_PATH}"}} > 90',
                            ]
                        )
                        + f") and on(namespace) ({expected})",
                        for_="2m",
                        labels={"severity": "warning"},
                        summary="Piraeus thin-pool metrics are stale",
                        description=(
                            f"The last successful sample or textfile update for {volume_group}/{thin_pool} is missing or older than 90 seconds."
                        ),
                    ),
                    Rule.alert(
                        "PiraeusThinPoolScrapeMissing",
                        f"((up{{{target}}} == 0) or absent(up{{{target}}})) and on(namespace) ({expected})",
                        for_="2m",
                        labels={"severity": "warning"},
                        summary="Piraeus thin-pool exporter is not being scraped",
                        description="Alloy has not scraped the Piraeus thin-pool exporter successfully for 2 minutes.",
                    ),
                ],
            )
        ],
    )
