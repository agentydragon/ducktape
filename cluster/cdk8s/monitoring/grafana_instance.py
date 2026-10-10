"""The Grafana instance (run by grafana-operator), its Postgres, route, datasources and
dashboards. A dashboard authored here is `dashboards/<name>.json` beside this module plus its
line in `_FILE_DASHBOARDS`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cdk8s import ApiObjectMetadata, App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from grafana_grafana_crds.org.integreatly.grafana import (
    GrafanaSpecClient,
    GrafanaSpecDeployment,
    GrafanaSpecDeploymentSpec,
    GrafanaSpecDeploymentSpecTemplate,
    GrafanaSpecDeploymentSpecTemplateSpec,
    GrafanaSpecDeploymentSpecTemplateSpecContainers,
    GrafanaSpecDeploymentSpecTemplateSpecContainersEnv,
    GrafanaSpecDeploymentSpecTemplateSpecContainersEnvValueFrom,
    GrafanaSpecDeploymentSpecTemplateSpecContainersEnvValueFromSecretKeyRef,
)
from grafana_grafanadashboard_crds.org.integreatly.grafana import (
    GrafanaDashboardSpecDatasources,
    GrafanaDashboardSpecGrafanaCom,
    GrafanaDashboardSpecInstanceSelector,
)
from grafana_grafanadatasource_crds.org.integreatly.grafana import (
    GrafanaDatasourceSpecDatasource,
    GrafanaDatasourceSpecInstanceSelector,
)

from cluster.cdk8s import cnpg, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.grafana_dashboards import DashboardFile
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import mimir
from cluster.cdk8s.providers.grafana_operator.grafana import Grafana
from cluster.cdk8s.providers.grafana_operator.grafana_dashboard import GrafanaDashboard
from cluster.cdk8s.providers.grafana_operator.grafana_datasource import GrafanaDatasource
from cluster.cdk8s.secret_ref import SecretKey, SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_NAME = "grafana"
_NAMESPACE = "monitoring"
HOSTNAME = "grafana.allegedly.works"
# The Service grafana-operator creates for the Grafana named `_NAME`.
_SERVICE = ServiceRef(
    name=f"{_NAME}-service",
    port=Port(name="grafana", number=3000),
    pods=Pods(namespace=_NAMESPACE, labels=(("app", _NAME),)),
)
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/grafana-instance"
DATABASE = cnpg.PostgresRef(
    name="grafana-db-ovh",
    namespace=_NAMESPACE,
    # The credentials CNPG generated for the retired `grafana-db`, which this cluster was cloned
    # from; the role's password came with the clone.
    app_secret=SecretRef(namespace=_NAMESPACE, name="grafana-db-app"),
)
_ADMIN = SecretRef(namespace=_NAMESPACE, name="grafana-admin-password")
_OIDC = SecretRef(namespace=_NAMESPACE, name="grafana-oidc-config")
# The label the Grafana CR carries and every dashboard and datasource selects.
_INSTANCE_LABELS = {"dashboards": _NAME}


@dataclass(frozen=True)
class _FileDashboard:
    # Maps each dashboard input to a datasource name.
    datasources: Mapping[str, str]
    folder: str | None = None


# `dashboards/<name>.json` beside this module, deployed as the `<name>-dashboard` ConfigMap.
_FILE_DASHBOARDS = {
    "flux-cluster": _FileDashboard(datasources={}),
    "interface-flap-frequency": _FileDashboard(datasources={"DS_LOKI": "Loki", "DS_PROMETHEUS": "Mimir"}),
    "rugged-power": _FileDashboard(datasources={"DS_PROMETHEUS": "Mimir"}),
    "cluster-storage-io": _FileDashboard(datasources={"DS_MIMIR": "Mimir", "DS_LOKI": "Loki"}),
    # Beside the AT&T gateway exporter's own "Home gateway" dashboard.
    "home-network-overview": _FileDashboard(datasources={}, folder="Home network"),
}


def _dashboard_file(name: str) -> DashboardFile:
    return DashboardFile(
        source=f"cluster/cdk8s/monitoring/dashboards/{name}.json", config_map=f"{name}-dashboard", namespace=_NAMESPACE
    )


DASHBOARDS = [_dashboard_file(name) for name in _FILE_DASHBOARDS]


def _database(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "database",
        ref=DATABASE,
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh",
        size="2Gi",
        # Created by pg_basebackup from the retired grafana-db, so this never initializes
        # anything. It names the application database and role CNPG uses: the metrics
        # exporter's default queries run against the database, and CNPG keeps the role's
        # password in sync with the Secret Grafana also authenticates with. Without it CNPG
        # defaults to `app`, which does not exist here.
        initdb=cnpg.same_owner_initdb(_NAME, secret=DATABASE.app_secret),
        wal_archive=False,
    )


def _secret_env(name: str, key: SecretKey) -> GrafanaSpecDeploymentSpecTemplateSpecContainersEnv:
    """The Grafana CRD's own env struct: its schema, not `k8s.EnvVar`."""
    return GrafanaSpecDeploymentSpecTemplateSpecContainersEnv(
        name=name,
        value_from=GrafanaSpecDeploymentSpecTemplateSpecContainersEnvValueFrom(
            secret_key_ref=GrafanaSpecDeploymentSpecTemplateSpecContainersEnvValueFromSecretKeyRef(
                name=key.secret.name, key=key.key
            )
        ),
    )


def _grafana(chart: Chart) -> None:
    Grafana(
        chart,
        "grafana",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE, labels=_INSTANCE_LABELS),
        client=GrafanaSpecClient(use_kube_auth=True),
        config={
            "server": {"root_url": f"https://{HOSTNAME}"},
            "security": {
                # Expanded at runtime from GF_SECURITY_ADMIN_{USER,PASSWORD} env vars below.
                # init-time only: Grafana writes the admin user to PostgreSQL on first boot.
                "admin_user": "${GF_SECURITY_ADMIN_USER}",
                "admin_password": "${GF_SECURITY_ADMIN_PASSWORD}",
            },
            "database": {
                "type": "postgres",
                "host": f"{DATABASE.rw.host}:{DATABASE.rw.port.number}",
                "name": _NAME,
                "user": _NAME,
                "password": "${GF_DATABASE_PASSWORD}",
                "ssl_mode": "require",
            },
            "plugins": {"preinstall": "grafana-clock-panel,grafana-clickhouse-datasource@4.20.0"},
            "auth.generic_oauth": {
                "enabled": "true",
                "name": "Authentik",
                "scopes": "openid email profile",
                "auth_url": "https://auth.allegedly.works/application/o/authorize/",
                "token_url": "http://authentik-server.authentik/application/o/token/",
                "api_url": "http://authentik-server.authentik/application/o/userinfo/",
                "client_id": "${GF_AUTH_GENERIC_OAUTH_CLIENT_ID}",
                "client_secret": "${GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET}",
                "role_attribute_path": "contains(groups[*], 'Grafana Admins') && 'Admin' || 'Viewer'",
                "allow_sign_up": "true",
            },
            "auth.jwt": {
                # Operator authenticates via K8s projected service account token (audience:
                # operator.grafana.com). No admin password required for automation.
                "enabled": "true",
                "header_name": "Authorization",
                "username_claim": "sub",
                "email_claim": "sub",
                # auto_sign_up: creates the operator's K8s identity as a Grafana user on first
                # auth. Scoped to JWT auth only — does not affect Authentik OAuth logins.
                "auto_sign_up": "true",
                "jwk_set_url": "https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT_HTTPS}/openid/v1/jwks",
                "jwk_set_bearer_token_file": "/var/run/secrets/kubernetes.io/serviceaccount/token",
                "expect_claims": '{"aud": "operator.grafana.com"}',
                "role_attribute_path": (
                    "contains(sub, 'system:serviceaccount:monitoring:grafana-operator') && 'GrafanaAdmin' || 'None'"
                ),
            },
        },
        deployment=GrafanaSpecDeployment(
            spec=GrafanaSpecDeploymentSpec(
                template=GrafanaSpecDeploymentSpecTemplate(
                    spec=GrafanaSpecDeploymentSpecTemplateSpec(
                        containers=[
                            GrafanaSpecDeploymentSpecTemplateSpecContainers(
                                name=_NAME,
                                env=[
                                    # Extend Go TLS trust to include the cluster CA (required for JWKS
                                    # endpoint verification). SSL_CERT_DIR is a Go standard env var —
                                    # overrides the default cert dir search list.
                                    GrafanaSpecDeploymentSpecTemplateSpecContainersEnv(
                                        name="SSL_CERT_DIR",
                                        value="/etc/ssl/certs:/var/run/secrets/kubernetes.io/serviceaccount",
                                    ),
                                    # Secret key names don't match GF_* env var names — explicit mapping.
                                    _secret_env("GF_SECURITY_ADMIN_USER", _ADMIN.key("admin-user")),
                                    _secret_env("GF_SECURITY_ADMIN_PASSWORD", _ADMIN.key("admin-password")),
                                    _secret_env("GF_DATABASE_PASSWORD", DATABASE.app_secret.key("password")),
                                    *(
                                        _secret_env(key, _OIDC.key(key))
                                        for key in (
                                            "GF_AUTH_GENERIC_OAUTH_CLIENT_ID",
                                            "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET",
                                        )
                                    ),
                                ],
                            )
                        ]
                    )
                )
            )
        ),
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        hostnames=[HOSTNAME],
        backend=_SERVICE,
        hsts=False,
        listener=None,
    )


def _datasource(chart: Chart, name: str, datasource: GrafanaDatasourceSpecDatasource) -> None:
    GrafanaDatasource(
        chart,
        f"datasource-{name}",
        metadata=ApiObjectMetadata(name=name, namespace=_NAMESPACE),
        instance_selector=GrafanaDatasourceSpecInstanceSelector(match_labels=_INSTANCE_LABELS),
        datasource=datasource,
    )


def _datasources(chart: Chart) -> None:
    _datasource(
        chart,
        "mimir",
        GrafanaDatasourceSpecDatasource(
            name="Mimir",
            uid="mimir",
            type="prometheus",
            access="proxy",
            url=f"{mimir.GATEWAY_URL}/prometheus",
            is_default=True,
            editable=True,
        ),
    )
    _datasource(
        chart,
        "loki",
        GrafanaDatasourceSpecDatasource(
            name="Loki",
            type="loki",
            access="proxy",
            # SimpleScalable: reads go to the read Deployment's Service.
            # The single-binary `loki` Service was removed when we switched
            # off SingleBinary mode.
            url="http://loki-read.loki.svc.cluster.local:3100",
            is_default=False,
            editable=True,
            json_data={"maxLines": 1000},
        ),
    )
    _datasource(
        chart,
        "tempo",
        GrafanaDatasourceSpecDatasource(
            name="Tempo",
            type="tempo",
            access="proxy",
            url="http://tempo.monitoring.svc.cluster.local:3200",
            is_default=False,
            editable=True,
            json_data={
                "httpMethod": "GET",
                "tracesToLogsV2": {
                    "datasourceUid": "loki",
                    "spanStartTimeShift": "-1h",
                    "spanEndTimeShift": "1h",
                    "filterByTraceID": True,
                    "filterBySpanID": False,
                },
            },
        ),
    )
    # The Alertmanager that Mimir's ruler sends to: its alert groups and silences in
    # Grafana, without exposing Alertmanager itself.
    _datasource(
        chart,
        "alertmanager",
        GrafanaDatasourceSpecDatasource(
            name="Alertmanager",
            type="alertmanager",
            access="proxy",
            url="http://alertmanager-operated.monitoring.svc.cluster.local:9093",
            is_default=False,
            editable=True,
            json_data={"implementation": "prometheus", "handleGrafanaManagedAlerts": False},
        ),
    )


def _dashboard(
    chart: Chart,
    name: str,
    *,
    datasources: Mapping[str, str] | None = None,
    folder: str | None = None,
    config_map: DashboardFile | None = None,
    grafana_com: GrafanaDashboardSpecGrafanaCom | None = None,
) -> None:
    """`datasources` maps each dashboard input to a datasource name."""
    GrafanaDashboard(
        chart,
        f"dashboard-{name}",
        metadata=ApiObjectMetadata(name=name, namespace=_NAMESPACE),
        instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels=_INSTANCE_LABELS),
        folder=folder,
        datasources=[
            GrafanaDashboardSpecDatasources(input_name=input_name, datasource_name=datasource)
            for input_name, datasource in datasources.items()
        ]
        if datasources
        else None,
        config_map_ref=config_map.config_map_ref() if config_map else None,
        grafana_com=grafana_com,
    )


def _dashboards(chart: Chart) -> None:
    for name, dashboard in _FILE_DASHBOARDS.items():
        _dashboard(
            chart, name, datasources=dashboard.datasources, folder=dashboard.folder, config_map=_dashboard_file(name)
        )
    _dashboard(
        chart,
        "cert-manager",
        datasources={"datasource": "Mimir"},
        grafana_com=GrafanaDashboardSpecGrafanaCom(id=20842, revision=3),
    )
    _dashboard(
        chart,
        "authentik",
        datasources={"DS_PROMETHEUS": "Mimir"},
        grafana_com=GrafanaDashboardSpecGrafanaCom(id=14837, revision=2),
    )
    _dashboard(
        chart,
        "cloudnative-pg",
        datasources={"DS_PROMETHEUS": "Mimir"},
        grafana_com=GrafanaDashboardSpecGrafanaCom(id=20417, revision=3),
    )
    _dashboard(
        chart,
        "cilium-agent",
        datasources={"DS_PROMETHEUS": "Mimir"},
        grafana_com=GrafanaDashboardSpecGrafanaCom(id=16611, revision=1),
    )
    _dashboard(
        chart,
        "etcd",
        datasources={"DS_PROMETHEUS": "Mimir"},
        grafana_com=GrafanaDashboardSpecGrafanaCom(id=3070, revision=3),
    )
    # NVIDIA DCGM Exporter dashboard (power/temp/clocks, PCIe, XID). Backed by the
    # DCGM metrics from dcgm_exporter/exporter.py via Mimir.
    _dashboard(
        chart,
        "gpu",
        datasources={"DS_PROMETHEUS": "Mimir"},
        grafana_com=GrafanaDashboardSpecGrafanaCom(id=12239, revision=2),
    )
    # Upstream Node Exporter Full dashboard. Its Hardware Misc row includes
    # hwmon temperature thresholds and fan-speed panels for every node-exporter
    # target; the datasource binding points those panels at Mimir.
    _dashboard(
        chart,
        "node-exporter-full",
        datasources={"ds_prometheus": "Mimir"},
        grafana_com=GrafanaDashboardSpecGrafanaCom(id=1860, revision=45),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, "grafana-instance", disable_resource_name_hashes=True)
    _database(chart)
    _grafana(chart)
    _datasources(chart)
    _dashboards(chart)
    return chart


def grafana_instance(
    flux_chart: Chart, directory: RenderedDirectory, grafana_operator: Kustomization, cnpg: Kustomization
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "grafana-instance",
        directory,
        wait=None,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="postgresql.cnpg.io/v1", kind="Cluster", name=DATABASE.name, namespace=_NAMESPACE
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name=f"{_NAME}-deployment", namespace=_NAMESPACE
            ),
        ],
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(grafana_operator, cnpg),
    )
