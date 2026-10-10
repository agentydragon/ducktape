"""The AT&T gateway exporter (`att_gateway/`): its Deployment on a node on
the gateway's LAN, Service, ServiceMonitor and the "Home gateway" dashboard
(`dashboard.json` beside this module). Also the CronJob running the image's syslog
reconciler, which keeps the gateway sending its firewall log to alloy-syslog.

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
from pydantic_settings import BaseSettings

from att_gateway.settings import Settings, SyslogLevel, SyslogSettings
from cluster.cdk8s import pod_policy
from cluster.cdk8s.att_gateway_exporter import access_code
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry import chart as forgejo_images
from cluster.cdk8s.grafana_dashboards import DashboardFile
from cluster.cdk8s.home_lan import HOME_LAN
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.monitoring import alloy
from cluster.cdk8s.node_scheduling import OPTIPLEX
from cluster.cdk8s.providers.grafana_operator.grafana_dashboard import GrafanaDashboard
from cluster.cdk8s.providers.prometheus_operator.service_monitor import ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from util.settings_contract import env_name

NAME = "att-gateway-exporter"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
_NAMESPACE = "monitoring"
# Only home-LAN nodes reach the gateway.
_GATEWAY_URL = f"http://{HOME_LAN.gateway}"
_HTTP = Port(name="http", number=9173)
_SERVICE = ServiceRef(
    name=NAME, port=_HTTP, pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", NAME),))
)
# The tag is a placeholder: `PINS_DIR` sets the real one.
_IMAGE = f"git.allegedly.works/ducktape-ci/{NAME}:unset"
DASHBOARD = DashboardFile(
    source="cluster/cdk8s/att_gateway_exporter/dashboard.json", config_map=f"{NAME}-dashboard", namespace=_NAMESPACE
)


_RESOURCES = k8s.ResourceRequirements(
    requests={"cpu": k8s.Quantity.from_string("10m"), "memory": k8s.Quantity.from_string("64Mi")},
    limits={"cpu": k8s.Quantity.from_string("200m"), "memory": k8s.Quantity.from_string("128Mi")},
)


def _gateway_env(settings: type[BaseSettings]) -> list[k8s.EnvVar]:
    return [
        k8s.EnvVar(name=env_name(settings, "url"), value=_GATEWAY_URL),
        access_code.ACCESS_CODE.env_var(env_name(settings, "access_code")),
    ]


def _pod_spec(
    container: k8s.Container, *, restart_policy: str | None = None, termination_grace_period_seconds: int | None = None
) -> k8s.PodSpec:
    """The exporter's and the reconciler's pods: one container running `_IMAGE`."""
    return k8s.PodSpec(
        restart_policy=restart_policy,
        # The pull secret the github-exporter chart provisions in this namespace.
        image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
        automount_service_account_token=False,
        termination_grace_period_seconds=termination_grace_period_seconds,
        security_context=k8s.PodSecurityContext(run_as_non_root=True, run_as_user=65532, run_as_group=65532),
        containers=[container],
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
                spec=_pod_spec(
                    k8s.Container(
                        name="exporter",
                        image=_IMAGE,
                        image_pull_policy="IfNotPresent",
                        # No readOnlyRootFilesystem: the aspect_rules_py launcher
                        # materialises its venv inside the image at startup.
                        env=[
                            *_gateway_env(Settings),
                            k8s.EnvVar(name=env_name(Settings, "listen_port"), value=str(_HTTP.number)),
                        ],
                        ports=[_HTTP.k8s_container_port()],
                        # /metrics serves the cache, so probing it never reaches the gateway.
                        liveness_probe=tcp,
                        readiness_probe=tcp,
                        resources=_RESOURCES,
                    ),
                    termination_grace_period_seconds=10,
                ),
            ),
        ),
    )


def _syslog_cron_job(chart: Chart) -> k8s.KubeCronJob:
    return k8s.KubeCronJob(
        chart,
        "syslog",
        metadata=k8s.ObjectMeta(
            name=f"{NAME}-syslog",
            namespace=_NAMESPACE,
            annotations={"description": "Keeps the AT&T gateway sending its firewall log to alloy-syslog (syslog.ha)."},
        ),
        spec=k8s.CronJobSpec(
            schedule="*/10 * * * *",
            concurrency_policy="Forbid",
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    backoff_limit=0,
                    active_deadline_seconds=300,
                    template=k8s.PodTemplateSpec(
                        spec=_pod_spec(
                            k8s.Container(
                                name="reconcile",
                                image=_IMAGE,
                                image_pull_policy="IfNotPresent",
                                # The image's second binary (att_gateway/BUILD.bazel).
                                command=["/att_gateway/syslog_reconciler_image_bin"],
                                env=[
                                    *_gateway_env(SyslogSettings),
                                    *(
                                        k8s.EnvVar(name=env_name(SyslogSettings, "syslog", field), value=value)
                                        for field, value in (
                                            ("enabled", "true"),
                                            ("server", str(HOME_LAN.optiplex)),
                                            ("port", str(alloy.GATEWAY_SYSLOG_HOST_PORT)),
                                            # The most inclusive level.
                                            ("level", str(SyslogLevel.NOTICE)),
                                        )
                                    ),
                                ],
                                resources=_RESOURCES,
                            ),
                            restart_policy="Never",
                        )
                    ),
                )
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for workload in (_deployment(chart), _syslog_cron_job(chart)):
        pod_policy.harden(workload)
        pod_policy.place(workload, OPTIPLEX)
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
    access_code_secret: Kustomization,
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
            access_code_secret,
            # ServiceMonitor
            monitoring_crds,
            # GrafanaDashboard
            grafana_operator,
        ),
        description="AT&T gateway metrics: WAN, fiber optics, LAN ports, NAT sessions, speed tests.",
    )
