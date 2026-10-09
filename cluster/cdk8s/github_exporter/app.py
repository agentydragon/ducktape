"""GitHub API rate-limit exporters for the human and agent accounts: the upstream REST
exporter and our GraphQL one, their Services, ServiceMonitors, token ExternalSecrets and
the Grafana dashboard (`dashboard.json` beside this module).

The GraphQL exporter's image tag is a placeholder; the hand-written `PINS_DIR` Component,
which the kustomization includes across the roots, sets it via Flux's image-automation marker.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)
from grafana_grafanadashboard_crds.org.integreatly.grafana import GrafanaDashboardSpecInstanceSelector
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsRelabelings,
    ServiceMonitorSpecEndpointsScheme,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s import external_creds
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry import chart as forgejo_images
from cluster.cdk8s.grafana_dashboards import DashboardFile
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.providers.grafana_operator.grafana_dashboard import GrafanaDashboard
from cluster.cdk8s.providers.prometheus_operator.service_monitor import ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{GENERATED_ROOT}/github-exporter"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/github-exporter-image-pins"
_NAMESPACE = "monitoring"
_ACCOUNTS = ("agentydragon", "agentydragon-agent")
# The external-creds source Secret each account's token is copied from.
_TOKEN_SOURCES = {"agentydragon": "github-agentydragon-2", "agentydragon-agent": "github-agentydragon-agent"}
_REST = "github-exporter"
_GRAPHQL = "github-graphql-rate-exporter"
_REST_HTTP = Port(name="http", number=9171)
_GRAPHQL_HTTP = Port(name="http", number=9172)
# The tag is a placeholder: `PINS_DIR` sets the real one.
_GRAPHQL_IMAGE = "git.allegedly.works/ducktape-ci/github-graphql-rate-exporter:unset"
_TOKEN_DIR = "/var/run/secrets/github"
DASHBOARD = DashboardFile(
    source="cluster/cdk8s/github_exporter/dashboard.json", config_map="github-exporter-dashboard", namespace=_NAMESPACE
)


def _token_secret(account: str) -> str:
    return f"github-exporter-{account}-token"


def _app_labels(app: str) -> dict[str, str]:
    """Every account's exporter of `app`: its ServiceMonitor selects these."""
    return {"app.kubernetes.io/name": app, "app.kubernetes.io/component": "quota"}


def _service(app: str, port: Port, account: str) -> ServiceRef:
    """One account's exporter of `app`."""
    return ServiceRef(
        name=f"{app}-{account}",
        port=port,
        pods=Pods(namespace=_NAMESPACE, labels=(*_app_labels(app).items(), ("github_account", account))),
    )


def _token_external_secret(chart: Chart, account: str) -> None:
    ExternalSecret(
        chart,
        f"token-{account}",
        metadata=ApiObjectMetadata(name=_token_secret(account), namespace=_NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data(_TOKEN_SOURCES[account], "token")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(type="Opaque"),
    )


def _deployment(
    chart: Chart,
    service: ServiceRef,
    *,
    account: str,
    description: str,
    container: k8s.Container,
    image_pull_secrets: list[k8s.LocalObjectReference] | None = None,
) -> None:
    labels = service.pods.selector
    k8s.KubeDeployment(
        chart,
        service.name,
        metadata=k8s.ObjectMeta(
            name=service.name,
            namespace=_NAMESPACE,
            labels=labels,
            annotations={"description": description, "secret.reloader.stakater.com/reload": _token_secret(account)},
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            revision_history_limit=2,
            selector=k8s.LabelSelector(match_labels=labels),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
                spec=k8s.PodSpec(
                    image_pull_secrets=image_pull_secrets,
                    automount_service_account_token=False,
                    termination_grace_period_seconds=10,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65532,
                        run_as_group=65532,
                        fs_group=65532,
                        fs_group_change_policy="OnRootMismatch",
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[container],
                    volumes=[
                        k8s.Volume(
                            name="github-token",
                            secret=k8s.SecretVolumeSource(
                                secret_name=_token_secret(account),
                                default_mode=0o440,
                                items=[k8s.KeyToPath(key="token", path="token")],
                            ),
                        )
                    ],
                ),
            ),
        ),
    )


def _resources(memory_request: str, memory_limit: str) -> k8s.ResourceRequirements:
    return k8s.ResourceRequirements(
        requests={"cpu": k8s.Quantity.from_string("10m"), "memory": k8s.Quantity.from_string(memory_request)},
        limits={"cpu": k8s.Quantity.from_string("100m"), "memory": k8s.Quantity.from_string(memory_limit)},
    )


def _rest_exporter(chart: Chart, service: ServiceRef, account: str) -> None:
    tcp_probe = k8s.TcpSocketAction(port=k8s.IntOrString.from_string(service.port.name))
    _deployment(
        chart,
        service,
        account=account,
        description=f"GitHub API rate-limit exporter for the {account} account.",
        container=k8s.Container(
            name="exporter",
            image="githubexporter/github-exporter:v2.3.1",
            image_pull_policy="IfNotPresent",
            security_context=k8s.SecurityContext(
                allow_privilege_escalation=False,
                read_only_root_filesystem=True,
                capabilities=k8s.Capabilities(drop=["ALL"]),
            ),
            env=[
                k8s.EnvVar(name="GITHUB_TOKEN_FILE", value=f"{_TOKEN_DIR}/token"),
                k8s.EnvVar(name="GITHUB_RATE_LIMIT_ENABLED", value="true"),
                k8s.EnvVar(name="FETCH_REPO_RELEASES_ENABLED", value="false"),
                k8s.EnvVar(name="LISTEN_PORT", value=str(service.pod_port)),
                k8s.EnvVar(name="LOG_LEVEL", value="info"),
            ],
            ports=[service.port.k8s_container_port()],
            volume_mounts=[k8s.VolumeMount(name="github-token", mount_path=_TOKEN_DIR, read_only=True)],
            liveness_probe=k8s.Probe(tcp_socket=tcp_probe, initial_delay_seconds=10, period_seconds=30),
            readiness_probe=k8s.Probe(tcp_socket=tcp_probe, initial_delay_seconds=5, period_seconds=30),
            resources=_resources("32Mi", "64Mi"),
        ),
    )


def _graphql_exporter(chart: Chart, service: ServiceRef, account: str) -> None:
    # /healthz answers locally; probing /metrics would call GitHub on every probe.
    healthz = k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string(service.port.name))
    _deployment(
        chart,
        service,
        account=account,
        description=f"GitHub GraphQL rate-limit exporter for the {account} account.",
        # The upstream exporter beside this one pulls from Docker Hub; this image comes
        # from the private Forgejo registry and needs the pull secret.
        image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
        container=k8s.Container(
            name="exporter",
            image=_GRAPHQL_IMAGE,
            image_pull_policy="IfNotPresent",
            security_context=k8s.SecurityContext(
                allow_privilege_escalation=False,
                # No readOnlyRootFilesystem, unlike the exporter beside this one: the
                # aspect_rules_py launcher materialises its venv at startup inside the
                # image's own runfiles directory, so the root filesystem has to be
                # writable or the container exits before serving anything.
                capabilities=k8s.Capabilities(drop=["ALL"]),
            ),
            env=[
                k8s.EnvVar(name="GITHUB_ACCOUNT", value=account),
                k8s.EnvVar(name="GITHUB_TOKEN_FILE", value=f"{_TOKEN_DIR}/token"),
            ],
            ports=[service.port.k8s_container_port()],
            volume_mounts=[k8s.VolumeMount(name="github-token", mount_path=_TOKEN_DIR, read_only=True)],
            liveness_probe=k8s.Probe(http_get=healthz, initial_delay_seconds=10, period_seconds=30),
            readiness_probe=k8s.Probe(http_get=healthz, initial_delay_seconds=5, period_seconds=30),
            resources=_resources("64Mi", "128Mi"),
        ),
    )


def _kube_service(chart: Chart, service: ServiceRef) -> None:
    k8s.KubeService(
        chart,
        f"{service.name}-service",
        metadata=k8s.ObjectMeta(name=service.name, namespace=_NAMESPACE, labels=service.labels),
        spec=k8s.ServiceSpec(selector=service.pods.selector, ports=[service.port.k8s_service_port()]),
    )


def _service_monitor(chart: Chart, app: str, endpoint: ServiceMonitorSpecEndpoints) -> None:
    ServiceMonitor(
        chart,
        f"{app}-monitor",
        metadata=ApiObjectMetadata(name=app, namespace=_NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=_app_labels(app)),
        endpoints=[endpoint],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, "github-exporter", disable_resource_name_hashes=True)
    forgejo_images.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    # The exporters receive namespace-local copies of the centralized PATs through
    # their source-approved credential edges.
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=_NAMESPACE)
    )
    for account in _ACCOUNTS:
        _token_external_secret(chart, account)
        rest = _service(_REST, _REST_HTTP, account)
        _rest_exporter(chart, rest, account)
        _kube_service(chart, rest)
    # REST /rate_limit misreports the GraphQL bucket: it served
    # `graphql: {remaining: 5000, used: 0}` in the same second that GraphQL itself
    # reported `remaining: 0, used: 10783` and every GraphQL call returned 403. So
    # `github_rate_*{resource="graphql"}` from the upstream exporter next to this one
    # cannot be used, and this exporter reads the `x-ratelimit-*` headers off a real
    # GraphQL response instead.
    #
    # Its probe is `query { rateLimit { cost } }`. A rateLimit-only query costs 0
    # points and GitHub keeps serving it while the account is exhausted, so scraping
    # every minute neither consumes the quota it measures nor goes blind at the
    # moment the quota runs out.
    for account in _ACCOUNTS:
        graphql = _service(_GRAPHQL, _GRAPHQL_HTTP, account)
        _graphql_exporter(chart, graphql, account)
        _kube_service(chart, graphql)
    _service_monitor(
        chart,
        _REST,
        ServiceMonitorSpecEndpoints(
            port=_REST_HTTP.name,
            path="/metrics",
            scheme=ServiceMonitorSpecEndpointsScheme.HTTP,
            scrape_timeout="15s",
            relabelings=[
                ServiceMonitorSpecEndpointsRelabelings(
                    source_labels=["__meta_kubernetes_service_label_github_account"], target_label="github_account"
                )
            ],
        ),
    )
    _service_monitor(
        chart,
        _GRAPHQL,
        ServiceMonitorSpecEndpoints(
            port=_GRAPHQL_HTTP.name,
            path="/metrics",
            scheme=ServiceMonitorSpecEndpointsScheme.HTTP,
            scrape_timeout="15s",
        ),
    )
    GrafanaDashboard(
        chart,
        "dashboard",
        metadata=ApiObjectMetadata(name="github-exporter", namespace=_NAMESPACE),
        instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels={"dashboards": "grafana"}),
        folder="GitHub",
        config_map_ref=DASHBOARD.config_map_ref(),
    )
    return chart


def github_exporter(
    flux_chart: Chart,
    directory: RenderedDirectory,
    monitoring_namespace: Kustomization,
    monitoring_crds: Kustomization,
    external_secrets_operator: Kustomization,
    grafana_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "github-exporter",
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            monitoring_namespace,
            # ServiceMonitor
            monitoring_crds,
            external_secrets_operator,
            # GrafanaDashboard
            grafana_operator,
        ),
        description="GitHub API rate-limit metrics for the human and agent accounts.",
    )
