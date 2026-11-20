"""The Grafana instance, its Postgres, route, datasources and dashboards."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cdk8s import ApiObjectMetadata, App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from grafana_grafana_crds.org.integreatly.grafana import (
    Grafana,
    GrafanaSpec,
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
    GrafanaDashboard,
    GrafanaDashboardSpec,
    GrafanaDashboardSpecDatasources,
    GrafanaDashboardSpecGrafanaCom,
    GrafanaDashboardSpecInstanceSelector,
)
from grafana_grafanadatasource_crds.org.integreatly.grafana import (
    GrafanaDatasource,
    GrafanaDatasourceSpec,
    GrafanaDatasourceSpecDatasource,
    GrafanaDatasourceSpecInstanceSelector,
)
from python.runfiles import Runfiles

from cluster.cdk8s.cnpg import backup, cluster
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import mimir
from cluster.cdk8s.monitoring.namespace import NAMESPACE
from cluster.cdk8s.secret_ref import SecretKey, SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_DASHBOARD_DIR = Runfiles.CreateOrRaise().root() / "ducktape/cluster/cdk8s/monitoring/dashboards"
_NAME = "grafana"
HOSTNAME = "grafana.allegedly.works"
GRAFANA_BASE_URL = f"https://{HOSTNAME}"
# Adopt Tempo's existing Grafana identity so saved references continue to resolve.
TEMPO_DATASOURCE_UID = "f99d1a97-7e72-4887-9277-0b105a5ccf44"
# Adopt the existing Grafana UID, originally derived from the Loki CR's metadata.uid.
# Keep this identity stable: changing it makes the operator delete the old datasource.
# The existing CR cannot gain immutable spec.uid; use the supported legacy datasource.uid.
LOKI_DATASOURCE_UID = "fc9a18f9-6955-4e40-95c1-66e32e7e8431"
# The Service grafana-operator creates for the Grafana named `_NAME`.
SERVICE = ServiceRef(
    name=f"{_NAME}-service",
    port=Port(name="grafana", number=3000),
    pods=Pods(namespace=NAMESPACE, labels=(("app", _NAME),)),
)
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/grafana-instance"
DATABASE = cluster.PostgresRef(
    name="grafana-db-ovh",
    namespace=NAMESPACE,
    # The credentials CNPG generated for the retired `grafana-db`, which this cluster was cloned
    # from; the role's password came with the clone.
    app_secret=SecretRef(namespace=NAMESPACE, name="grafana-db-app"),
)
_ADMIN = SecretRef(namespace=NAMESPACE, name="grafana-admin-password")
_OIDC = SecretRef(namespace=NAMESPACE, name="grafana-oidc-config")
# The label the Grafana CR carries and every dashboard and datasource selects.
INSTANCE_LABELS = {"dashboards": _NAME}


def _database(chart: Chart) -> None:
    cluster.cluster(
        chart,
        "database",
        ref=DATABASE,
        profile=cluster.OVH_PIRAEUS_SSD,
        size="4Gi",
        resize_in_use_volumes=False,
        # Created by pg_basebackup from the retired grafana-db, so this never initializes
        # anything. It names the application database and role CNPG uses: the metrics
        # exporter's default queries run against the database, and CNPG keeps the role's
        # password in sync with the Secret Grafana also authenticates with. Without it CNPG
        # defaults to `app`, which does not exist here.
        initdb=cluster.same_owner_initdb(_NAME, secret=DATABASE.app_secret),
        wal_archive=True,
    )
    backup.CnpgBackup(chart, "db-backup", ref=DATABASE, retention="30d")


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
        metadata=ApiObjectMetadata(name=_NAME, namespace=NAMESPACE, labels=INSTANCE_LABELS),
        spec=GrafanaSpec(
            client=GrafanaSpecClient(use_kube_auth=True),
            config={
                "server": {"root_url": GRAFANA_BASE_URL},
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
        ),
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=_NAME, namespace=NAMESPACE),
        hostnames=[HOSTNAME],
        backend=SERVICE,
        hsts=False,
        listener=None,
    )


def _datasource(chart: Chart, name: str, datasource: GrafanaDatasourceSpecDatasource) -> None:
    GrafanaDatasource(
        chart,
        f"datasource-{name}",
        metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
        spec=GrafanaDatasourceSpec(
            instance_selector=GrafanaDatasourceSpecInstanceSelector(match_labels=INSTANCE_LABELS), datasource=datasource
        ),
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
            uid=LOKI_DATASOURCE_UID,
            type="loki",
            access="proxy",
            # SimpleScalable: reads go to the read Deployment's Service.
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
            uid=TEMPO_DATASOURCE_UID,
            type="tempo",
            access="proxy",
            url="http://tempo.monitoring.svc.cluster.local:3200",
            is_default=False,
            editable=True,
            json_data={
                "httpMethod": "GET",
                "tracesToLogsV2": {
                    "datasourceUid": LOKI_DATASOURCE_UID,
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
    json: str | None = None,
    grafana_com: GrafanaDashboardSpecGrafanaCom | None = None,
) -> None:
    """`datasources` maps each input to its literal datasource name or UID substitution."""
    GrafanaDashboard(
        chart,
        f"dashboard-{name}",
        metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
        spec=GrafanaDashboardSpec(
            instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels=INSTANCE_LABELS),
            folder=folder,
            datasources=[
                GrafanaDashboardSpecDatasources(input_name=input_name, datasource_name=datasource)
                for input_name, datasource in datasources.items()
            ]
            if datasources
            else None,
            json=json,
            grafana_com=grafana_com,
        ),
    )


@dataclass(frozen=True)
class LocalDashboard:
    name: str
    folder: str
    datasources: tuple[tuple[str, str], ...]
    filename: str


@dataclass(frozen=True)
class GrafanaComDashboard:
    name: str
    folder: str
    datasources: tuple[tuple[str, str], ...]
    dashboard_id: int
    revision: int


type Dashboard = LocalDashboard | GrafanaComDashboard


_DASHBOARD_DEFINITIONS: tuple[Dashboard, ...] = (
    LocalDashboard("authentik", "Cluster & Platform", (("DS_MIMIR", "mimir"),), "authentik.json"),
    LocalDashboard("buildbuddy-executor-pool", "CI", (("DS_MIMIR", "mimir"),), "buildbuddy-executor-pool.json"),
    LocalDashboard(
        "zot-cache-image-pulls",
        "Cluster & Platform",
        (("DS_LOKI", LOKI_DATASOURCE_UID), ("DS_MIMIR", "mimir")),
        "zot-cache-image-pulls.json",
    ),
    LocalDashboard(
        "interface-flap-frequency",
        "Network",
        (("DS_LOKI", LOKI_DATASOURCE_UID), ("DS_MIMIR", "mimir")),
        "interface-flap-frequency.json",
    ),
    LocalDashboard("node-memory", "Nodes & Hardware", (("DS_MIMIR", "mimir"),), "node-memory.json"),
    LocalDashboard(
        "cluster-storage-io",
        "Storage",
        (("DS_LOKI", LOKI_DATASOURCE_UID), ("DS_MIMIR", "mimir")),
        "cluster-storage-io.json",
    ),
    LocalDashboard("s3-operations", "Storage", (("DS_MIMIR", "mimir"),), "s3-operations.json"),
    LocalDashboard("cluster-disk-usage", "Storage", (("DS_MIMIR", "mimir"),), "cluster-disk-usage.json"),
    LocalDashboard("piraeus-storage", "Storage", (("DS_MIMIR", "mimir"),), "piraeus-storage.json"),
    LocalDashboard("storage-service-details", "Storage", (("DS_MIMIR", "mimir"),), "storage-service-details.json"),
    LocalDashboard("io-pressure", "Storage", (("DS_MIMIR", "mimir"),), "io-pressure.json"),
    LocalDashboard(
        "home-network-overview",
        "Network",
        (("DS_LOKI", LOKI_DATASOURCE_UID), ("DS_MIMIR", "mimir")),
        "home-network-overview.json",
    ),
    LocalDashboard("cpap-gateway-disk", "Cluster & Platform", (("DS_MIMIR", "mimir"),), "cpap-gateway-disk.json"),
    LocalDashboard("flux-cluster", "Cluster & Platform", (("DS_MIMIR", "mimir"),), "flux-cluster.json"),
    LocalDashboard("rugged-power", "Nodes & Hardware", (("DS_MIMIR", "mimir"),), "rugged-power.json"),
    GrafanaComDashboard("cert-manager", "Cluster & Platform", (("datasource", "Mimir"),), 20842, 3),
    GrafanaComDashboard("cloudnative-pg", "Cluster & Platform", (("DS_PROMETHEUS", "Mimir"),), 20417, 3),
    LocalDashboard("cilium-agent", "Network", (("DS_MIMIR", "mimir"),), "cilium-agent.json"),
    LocalDashboard("etcd", "Cluster & Platform", (("DS_MIMIR", "mimir"),), "etcd.json"),
    LocalDashboard("gpu", "Nodes & Hardware", (("DS_MIMIR", "mimir"),), "gpu.json"),
    # Node Exporter Full includes hwmon thresholds and fan-speed panels for every node-exporter target.
    GrafanaComDashboard("node-exporter-full", "Nodes & Hardware", (("ds_prometheus", "Mimir"),), 1860, 45),
)


def _dashboards(chart: Chart) -> None:
    for dashboard in _DASHBOARD_DEFINITIONS:
        if isinstance(dashboard, LocalDashboard):
            _dashboard(
                chart,
                dashboard.name,
                datasources=dict(dashboard.datasources),
                folder=dashboard.folder,
                json=(_DASHBOARD_DIR / dashboard.filename).read_text(),
            )
        else:
            _dashboard(
                chart,
                dashboard.name,
                datasources=dict(dashboard.datasources),
                folder=dashboard.folder,
                grafana_com=GrafanaDashboardSpecGrafanaCom(id=dashboard.dashboard_id, revision=dashboard.revision),
            )


def chart(app: App) -> Chart:
    chart = Chart(app, "grafana-instance", disable_resource_name_hashes=True)
    _database(chart)
    _grafana(chart)
    _datasources(chart)
    _dashboards(chart)
    return chart


def grafana_instance(
    flux_chart: Chart,
    directory: RenderedDirectory,
    grafana_operator: Kustomization,
    cnpg: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "grafana-instance",
        directory,
        wait=None,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="postgresql.cnpg.io/v1", kind="Cluster", name=DATABASE.name, namespace=NAMESPACE
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name=f"{_NAME}-deployment", namespace=NAMESPACE
            ),
        ],
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(grafana_operator, cnpg, seaweedfs_operator),
    )
