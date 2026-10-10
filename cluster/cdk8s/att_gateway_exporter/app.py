"""The AT&T gateway exporter (`cluster/exporters/att_gateway/`): its Deployment on a node on
the gateway's LAN, Service, ServiceMonitor and the "Home gateway" dashboard
(`dashboard.json` beside this module).

The image tag is a placeholder; the hand-written `PINS_DIR` Component sets it via Flux's
image-automation marker.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from grafana_grafanadashboard_crds.org.integreatly.grafana import GrafanaDashboardSpecInstanceSelector
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsScheme,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s import pod_policy
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry import chart as forgejo_images
from cluster.cdk8s.grafana_dashboards import DashboardFile
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.node_scheduling import OPTIPLEX
from cluster.cdk8s.providers.grafana_operator.grafana_dashboard import GrafanaDashboard
from cluster.cdk8s.providers.prometheus_operator.service_monitor import ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.exporters.att_gateway.settings import Settings
from util.settings_contract import env_name

NAME = "att-gateway-exporter"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
_NAMESPACE = "monitoring"
# The BGW320's fixed LAN address; only home-LAN nodes reach it.
_GATEWAY_URL = "http://192.168.1.254"
_HTTP = Port(name="http", number=9173)
_SERVICE = ServiceRef(
    name=NAME, port=_HTTP, pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", NAME),))
)
# The tag is a placeholder: `PINS_DIR` sets the real one.
_IMAGE = f"git.allegedly.works/ducktape-ci/{NAME}:unset"
DASHBOARD = DashboardFile(
    source="cluster/cdk8s/att_gateway_exporter/dashboard.json", config_map=f"{NAME}-dashboard", namespace=_NAMESPACE
)


def _deployment(chart: Chart) -> k8s.KubeDeployment:
    labels = _SERVICE.pods.selector
    tcp = k8s.Probe(tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string(_HTTP.name)), period_seconds=30)
    return k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=_NAMESPACE,
            labels=labels,
            annotations={"description": "Prometheus metrics scraped from the AT&T BGW320 gateway's status pages."},
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            revision_history_limit=2,
            selector=k8s.LabelSelector(match_labels=labels),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
                spec=k8s.PodSpec(
                    # The pull secret the github-exporter chart provisions in this namespace.
                    image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
                    automount_service_account_token=False,
                    termination_grace_period_seconds=10,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True, run_as_user=65532, run_as_group=65532
                    ),
                    containers=[
                        k8s.Container(
                            name="exporter",
                            image=_IMAGE,
                            image_pull_policy="IfNotPresent",
                            # No readOnlyRootFilesystem: the aspect_rules_py launcher
                            # materialises its venv inside the image at startup.
                            env=[
                                k8s.EnvVar(name=env_name(Settings, "url"), value=_GATEWAY_URL),
                                k8s.EnvVar(name=env_name(Settings, "listen_port"), value=str(_HTTP.number)),
                            ],
                            ports=[_HTTP.k8s_container_port()],
                            # /metrics serves the cache, so probing it never reaches the gateway.
                            liveness_probe=tcp,
                            readiness_probe=tcp,
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("200m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                            ),
                        )
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    deployment = _deployment(chart)
    pod_policy.harden(deployment)
    pod_policy.place(deployment, OPTIPLEX)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=NAME, namespace=_NAMESPACE, labels=_SERVICE.labels),
        spec=k8s.ServiceSpec(selector=_SERVICE.pods.selector, ports=[_HTTP.k8s_service_port()]),
    )
    ServiceMonitor(
        chart,
        "monitor",
        metadata=ApiObjectMetadata(name=NAME, namespace=_NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=_SERVICE.labels),
        endpoints=[
            ServiceMonitorSpecEndpoints(
                port=_HTTP.name,
                path="/metrics",
                scheme=ServiceMonitorSpecEndpointsScheme.HTTP,
                # The exporter refreshes each page once a minute.
                interval="60s",
                scrape_timeout="10s",
            )
        ],
    )
    GrafanaDashboard(
        chart,
        "dashboard",
        metadata=ApiObjectMetadata(name=NAME, namespace=_NAMESPACE),
        instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels={"dashboards": "grafana"}),
        folder="Home network",
        config_map_ref=DASHBOARD.config_map_ref(),
    )
    return chart


def att_gateway_exporter(
    flux_chart: Chart,
    directory: RenderedDirectory,
    monitoring_namespace: Kustomization,
    monitoring_crds: Kustomization,
    grafana_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            monitoring_namespace,
            # ServiceMonitor
            monitoring_crds,
            # GrafanaDashboard
            grafana_operator,
        ),
        description="AT&T gateway metrics: WAN, fiber optics, LAN ports.",
    )
