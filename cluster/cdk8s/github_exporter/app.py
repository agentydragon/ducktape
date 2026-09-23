"""GitHub API rate-limit exporters for the human and agent accounts: the upstream REST
exporter and our GraphQL one, their Services, ServiceMonitors, token ExternalSecrets and
the Grafana dashboard.

Hand-written beside the generated output: `dashboard.json` (rendered into the
`github-exporter-dashboard` ConfigMap by the directory's `configMapGenerator`),
`kustomizeconfig` (which points the dashboard's `configMapRef` at the generated,
hash-suffixed ConfigMap name), the `kustomization.yaml` that wires both, and
`image-pins/`, whose Flux image-automation marker sets the GraphQL exporter's tag over
the placeholder below.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)
from grafana_grafanadashboard_crds.org.integreatly.grafana import (
    GrafanaDashboard,
    GrafanaDashboardSpec,
    GrafanaDashboardSpecConfigMapRef,
    GrafanaDashboardSpecInstanceSelector,
)
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsRelabelings,
    ServiceMonitorSpecEndpointsScheme,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s import forgejo_images
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

_OUTPUT_DIR = "cluster/k8s/github-exporter"
_NAMESPACE = "monitoring"
_ACCOUNTS = ("agentydragon", "agentydragon-agent")
# The external-creds source Secret each account's token is copied from.
_TOKEN_SOURCES = {"agentydragon": "github-agentydragon-2", "agentydragon-agent": "github-agentydragon-agent"}
_REST = "github-exporter"
_GRAPHQL = "github-graphql-rate-exporter"
_REST_PORT = 9171
_GRAPHQL_PORT = 9172
# The tag is a placeholder: image-pins/kustomization.yaml sets the real one.
_GRAPHQL_IMAGE = "git.allegedly.works/ducktape-ci/github-graphql-rate-exporter:unset"
_TOKEN_DIR = "/var/run/secrets/github"
_HTTP = k8s.IntOrString.from_string("http")


def _token_secret(account: str) -> str:
    return f"github-exporter-{account}-token"


def _labels(app: str, account: str) -> dict[str, str]:
    return {"app.kubernetes.io/name": app, "app.kubernetes.io/component": "quota", "github_account": account}


def _token_external_secret(chart: Chart, account: str) -> None:
    ExternalSecret(
        chart,
        f"token-{account}",
        metadata=metadata(_token_secret(account), _NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
                name="kubernetes-external-creds-secret-store",
            ),
            target=ExternalSecretSpecTarget(
                name=_token_secret(account),
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
                template=ExternalSecretSpecTargetTemplate(type="Opaque"),
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key="token",
                    remote_ref=ExternalSecretSpecDataRemoteRef(key=_TOKEN_SOURCES[account], property="token"),
                )
            ],
        ),
    )


def _deployment(
    chart: Chart,
    *,
    app: str,
    account: str,
    description: str,
    container: k8s.Container,
    image_pull_secrets: list[k8s.LocalObjectReference] | None = None,
) -> None:
    labels = _labels(app, account)
    k8s.KubeDeployment(
        chart,
        f"{app}-{account}",
        metadata=k8s.ObjectMeta(
            name=f"{app}-{account}",
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


def _rest_exporter(chart: Chart, account: str) -> None:
    tcp_probe = k8s.TcpSocketAction(port=_HTTP)
    _deployment(
        chart,
        app=_REST,
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
                k8s.EnvVar(name="LISTEN_PORT", value=str(_REST_PORT)),
                k8s.EnvVar(name="LOG_LEVEL", value="info"),
            ],
            ports=[k8s.ContainerPort(name="http", container_port=_REST_PORT, protocol="TCP")],
            volume_mounts=[k8s.VolumeMount(name="github-token", mount_path=_TOKEN_DIR, read_only=True)],
            liveness_probe=k8s.Probe(tcp_socket=tcp_probe, initial_delay_seconds=10, period_seconds=30),
            readiness_probe=k8s.Probe(tcp_socket=tcp_probe, initial_delay_seconds=5, period_seconds=30),
            resources=_resources("32Mi", "64Mi"),
        ),
    )


def _graphql_exporter(chart: Chart, account: str) -> None:
    # /healthz answers locally; probing /metrics would call GitHub on every probe.
    healthz = k8s.HttpGetAction(path="/healthz", port=_HTTP)
    _deployment(
        chart,
        app=_GRAPHQL,
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
            ports=[k8s.ContainerPort(name="http", container_port=_GRAPHQL_PORT, protocol="TCP")],
            volume_mounts=[k8s.VolumeMount(name="github-token", mount_path=_TOKEN_DIR, read_only=True)],
            liveness_probe=k8s.Probe(http_get=healthz, initial_delay_seconds=10, period_seconds=30),
            readiness_probe=k8s.Probe(http_get=healthz, initial_delay_seconds=5, period_seconds=30),
            resources=_resources("64Mi", "128Mi"),
        ),
    )


def _service(chart: Chart, app: str, account: str, port: int) -> None:
    labels = _labels(app, account)
    k8s.KubeService(
        chart,
        f"{app}-{account}-service",
        metadata=k8s.ObjectMeta(name=f"{app}-{account}", namespace=_NAMESPACE, labels=labels),
        spec=k8s.ServiceSpec(
            selector=labels, ports=[k8s.ServicePort(name="http", port=port, target_port=_HTTP, protocol="TCP")]
        ),
    )


def _service_monitor(chart: Chart, app: str, endpoint: ServiceMonitorSpecEndpoints) -> None:
    ServiceMonitor(
        chart,
        f"{app}-monitor",
        metadata=metadata(app, _NAMESPACE),
        spec=ServiceMonitorSpec(
            selector=ServiceMonitorSpecSelector(
                match_labels={"app.kubernetes.io/name": app, "app.kubernetes.io/component": "quota"}
            ),
            endpoints=[endpoint],
        ),
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
        _rest_exporter(chart, account)
        _service(chart, _REST, account, _REST_PORT)
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
        _graphql_exporter(chart, account)
        _service(chart, _GRAPHQL, account, _GRAPHQL_PORT)
    _service_monitor(
        chart,
        _REST,
        ServiceMonitorSpecEndpoints(
            port="http",
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
            port="http", path="/metrics", scheme=ServiceMonitorSpecEndpointsScheme.HTTP, scrape_timeout="15s"
        ),
    )
    GrafanaDashboard(
        chart,
        "dashboard",
        metadata=metadata("github-exporter", _NAMESPACE),
        spec=GrafanaDashboardSpec(
            folder="GitHub",
            instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels={"dashboards": "grafana"}),
            config_map_ref=GrafanaDashboardSpecConfigMapRef(name="github-exporter-dashboard", key="dashboard.json"),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
