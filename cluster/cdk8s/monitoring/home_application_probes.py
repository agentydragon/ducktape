"""Probe direct TCP and HTTPS connectivity from each home node."""

from __future__ import annotations

import yaml
from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecPodMetricsEndpointsRelabelings,
    PodMonitorSpecSelector,
)

from cluster.cdk8s import pod_policy
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on,
)
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import blackbox_exporter
from cluster.cdk8s.node_scheduling import Placement
from cluster.cdk8s.providers.prometheus_operator.pod_monitor import PodMonitor

NAME = "home-application-probes"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/{NAME}"
_LABELS = {"app.kubernetes.io/part-of": NAME}
_SOURCES = ("optiplex", "wyrm2")
_PORT_NAME = "http"
_PORT = 9115
_CONFIG_DIR = "/etc/blackbox_exporter"
_CONFIG_FILE = "blackbox.yml"
_UID = 65534
_TCP_MODULE = "tcp_ipv4"
_HTTPS_MODULE = "https_ipv4"

TARGETS = (
    ("1.1.1.1:443", _TCP_MODULE),
    ("https://data.forgejo.org/api/v1/version", _HTTPS_MODULE),
    ("https://github.com/robots.txt", _HTTPS_MODULE),
)

CONFIG = {
    "modules": {
        _TCP_MODULE: {
            "prober": "tcp",
            "timeout": "5s",
            "tcp": {"preferred_ip_protocol": "ip4", "ip_protocol_fallback": False},
        },
        _HTTPS_MODULE: {
            "prober": "http",
            "timeout": "5s",
            "http": {
                "method": "GET",
                "preferred_ip_protocol": "ip4",
                "ip_protocol_fallback": False,
                "follow_redirects": False,
                "fail_if_not_ssl": True,
                "proxy_from_environment": False,
                "tls_config": {"insecure_skip_verify": False},
                # Blackbox exporter reads at most 64 KiB of an uncompressed response body.
                "body_size_limit": "64KiB",
            },
        },
    }
}
CONFIG_MAP = ConfigMapArgs(name=f"{NAME}-config", namespace=NAMESPACE, data={_CONFIG_FILE: yaml.safe_dump(CONFIG)})


def _deployment(chart: Chart, source: str) -> k8s.KubeDeployment:
    name = NAME if source == _SOURCES[0] else f"{NAME}-{source}"
    selector = {"app.kubernetes.io/name": name}
    labels = {**selector, **_LABELS}
    return k8s.KubeDeployment(
        chart,
        f"deployment-{source}",
        metadata=k8s.ObjectMeta(
            name=name,
            namespace=NAMESPACE,
            labels=labels,
            annotations={"description": f"Direct TCP and HTTPS connectivity probes from {source}."},
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            revision_history_limit=2,
            selector=k8s.LabelSelector(match_labels=selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    termination_grace_period_seconds=10,
                    security_context=k8s.PodSecurityContext(run_as_user=_UID, run_as_group=_UID, run_as_non_root=True),
                    containers=[
                        k8s.Container(
                            name="blackbox-exporter",
                            image=blackbox_exporter.IMAGE,
                            image_pull_policy="IfNotPresent",
                            args=[f"--config.file={_CONFIG_DIR}/{_CONFIG_FILE}"],
                            ports=[k8s.ContainerPort(name=_PORT_NAME, container_port=_PORT)],
                            liveness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/-/healthy", port=k8s.IntOrString.from_string(_PORT_NAME)
                                ),
                                period_seconds=30,
                            ),
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/-/healthy", port=k8s.IntOrString.from_string(_PORT_NAME)
                                ),
                                period_seconds=10,
                            ),
                            security_context=k8s.SecurityContext(read_only_root_filesystem=True),
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path=_CONFIG_DIR, read_only=True)],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("64Mi")},
                            ),
                        )
                    ],
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=CONFIG_MAP.name))],
                ),
            ),
        ),
    )


def _endpoint(target: str, module: str) -> PodMonitorSpecPodMetricsEndpoints:
    return PodMonitorSpecPodMetricsEndpoints(
        port=_PORT_NAME,
        path="/probe",
        params={"module": [module], "target": [target]},
        interval="10s",
        scrape_timeout="7s",
        relabelings=[
            PodMonitorSpecPodMetricsEndpointsRelabelings(
                source_labels=["__meta_kubernetes_pod_node_name"], target_label="source_node"
            ),
            PodMonitorSpecPodMetricsEndpointsRelabelings(source_labels=["__param_target"], target_label="target"),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for source in _SOURCES:
        deployment = _deployment(chart, source)
        pod_policy.harden(deployment)
        pod_policy.place(deployment, Placement(node_selector={"kubernetes.io/hostname": source}))
    PodMonitor(
        chart,
        "pod-monitor",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        selector=PodMonitorSpecSelector(match_labels=_LABELS),
        pod_metrics_endpoints=[_endpoint(target, module) for target, module in TARGETS],
    )
    return chart


def home_application_probes(
    flux_chart: Chart, directory: RenderedDirectory, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        f"{NAMESPACE}-{NAME}",
        directory,
        timeout="5m",
        # The PodMonitor CRD is owned by monitoring-crds.
        depends_on=[flux_kustomization_depends_on(monitoring_crds)],
        description="Direct TCP and HTTPS connectivity probes from optiplex and wyrm2.",
    )
