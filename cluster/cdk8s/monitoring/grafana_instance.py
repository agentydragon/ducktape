"""The Grafana instance (run by grafana-operator), its Postgres, route, datasources and
dashboards.

Hand-written beside the generated output: the dashboard JSON files (each rendered into a
`<name>-dashboard` ConfigMap by the directory's `configMapGenerator`), `kustomizeconfig`
(which points a GrafanaDashboard's `configMapRef` at the generated, hash-suffixed
ConfigMap name) and the `kustomization.yaml` that wires both.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import ClusterSpecBootstrapInitdb, ClusterSpecBootstrapInitdbSecret
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
)
from grafana_grafana_crds.org.integreatly.grafana import (
    GrafanaSpec,
    GrafanaSpecClient,
    GrafanaSpecDeployment,
    GrafanaSpecDeploymentSpec,
    GrafanaSpecDeploymentSpecTemplate,
    GrafanaSpecDeploymentSpecTemplateMetadata,
    GrafanaSpecDeploymentSpecTemplateSpec,
    GrafanaSpecDeploymentSpecTemplateSpecContainers,
    GrafanaSpecDeploymentSpecTemplateSpecContainersEnv,
    GrafanaSpecDeploymentSpecTemplateSpecContainersEnvValueFrom,
    GrafanaSpecDeploymentSpecTemplateSpecContainersEnvValueFromSecretKeyRef,
)
from grafana_grafanadashboard_crds.org.integreatly.grafana import (
    GrafanaDashboardSpecConfigMapRef,
    GrafanaDashboardSpecDatasources,
    GrafanaDashboardSpecGrafanaCom,
)
from grafana_grafanadatasource_crds.org.integreatly.grafana import GrafanaDatasourceSpecDatasource

from cluster.cdk8s import cnpg
from cluster.cdk8s.gateway import cluster_gateway_parent_ref
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.grafana_operator.grafana import Grafana
from cluster.cdk8s.providers.grafana_operator.grafana_dashboard import GrafanaDashboard
from cluster.cdk8s.providers.grafana_operator.grafana_datasource import GrafanaDatasource

_NAME = "grafana"
_NAMESPACE = "monitoring"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/monitoring/grafana-instance"
_DB_NAME = "grafana-db-ovh"
# The credentials CNPG generated for the retired `grafana-db`, which this cluster was cloned
# from; the role's password came with the clone.
_DB_CREDENTIALS_SECRET = "grafana-db-app"
# The label the Grafana CR carries and every dashboard and datasource selects.
_INSTANCE_LABELS = {"dashboards": _NAME}


def _database(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "database",
        name=_DB_NAME,
        namespace=_NAMESPACE,
        node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
        storage_class="local-path-ovh",
        size="2Gi",
        # Created by pg_basebackup from the retired grafana-db, so this never initializes
        # anything. It names the application database and role CNPG uses: the metrics
        # exporter's default queries run against the database, and CNPG keeps the role's
        # password in sync with the Secret Grafana also authenticates with. Without it CNPG
        # defaults to `app`, which does not exist here.
        initdb=ClusterSpecBootstrapInitdb(
            database=_NAME, owner=_NAME, secret=ClusterSpecBootstrapInitdbSecret(name=_DB_CREDENTIALS_SECRET)
        ),
    )


def _secret_env(name: str, secret: str, key: str) -> GrafanaSpecDeploymentSpecTemplateSpecContainersEnv:
    return GrafanaSpecDeploymentSpecTemplateSpecContainersEnv(
        name=name,
        value_from=GrafanaSpecDeploymentSpecTemplateSpecContainersEnvValueFrom(
            secret_key_ref=GrafanaSpecDeploymentSpecTemplateSpecContainersEnvValueFromSecretKeyRef(name=secret, key=key)
        ),
    )


def _grafana(chart: Chart) -> None:
    Grafana(
        chart,
        "grafana",
        metadata=metadata(_NAME, _NAMESPACE, labels=_INSTANCE_LABELS),
        spec=GrafanaSpec(
            client=GrafanaSpecClient(use_kube_auth=True),
            config={
                "server": {"root_url": "https://grafana.allegedly.works"},
                "security": {
                    # Expanded at runtime from GF_SECURITY_ADMIN_{USER,PASSWORD} env vars below.
                    # init-time only: Grafana writes the admin user to PostgreSQL on first boot.
                    "admin_user": "${GF_SECURITY_ADMIN_USER}",
                    "admin_password": "${GF_SECURITY_ADMIN_PASSWORD}",
                },
                "database": {
                    "type": "postgres",
                    "host": f"{_DB_NAME}-rw.monitoring.svc.cluster.local:5432",
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
                        metadata=GrafanaSpecDeploymentSpecTemplateMetadata(
                            annotations={"reloader.stakater.com/auto": "true"}
                        ),
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
                                        _secret_env("GF_SECURITY_ADMIN_USER", "grafana-admin-password", "admin-user"),
                                        _secret_env(
                                            "GF_SECURITY_ADMIN_PASSWORD", "grafana-admin-password", "admin-password"
                                        ),
                                        _secret_env("GF_DATABASE_PASSWORD", _DB_CREDENTIALS_SECRET, "password"),
                                        _secret_env(
                                            "GF_AUTH_GENERIC_OAUTH_CLIENT_ID",
                                            "grafana-oidc-config",
                                            "GF_AUTH_GENERIC_OAUTH_CLIENT_ID",
                                        ),
                                        _secret_env(
                                            "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET",
                                            "grafana-oidc-config",
                                            "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET",
                                        ),
                                    ],
                                )
                            ]
                        ),
                    )
                )
            ),
        ),
    )
    HttpRoute(
        chart,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref()],
            hostnames=["grafana.allegedly.works"],
            rules=[HttpRouteSpecRules(backend_refs=[HttpRouteSpecRulesBackendRefs(name="grafana-service", port=3000)])],
        ),
    )


def _datasource(chart: Chart, name: str, datasource: GrafanaDatasourceSpecDatasource) -> None:
    GrafanaDatasource(
        chart,
        f"datasource-{name}",
        metadata=metadata(name, _NAMESPACE),
        instance_selector_labels=_INSTANCE_LABELS,
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
            url="http://mimir-gateway.monitoring.svc.cluster.local/prometheus",
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


def _dashboard(
    chart: Chart,
    name: str,
    *,
    datasources: dict[str, str] | None = None,
    config_map: bool = False,
    grafana_com: GrafanaDashboardSpecGrafanaCom | None = None,
) -> None:
    """`datasources` maps each dashboard input to a datasource name. A `config_map`
    dashboard reads `<name>.json` from the `<name>-dashboard` ConfigMap."""
    GrafanaDashboard(
        chart,
        f"dashboard-{name}",
        metadata=metadata(name, _NAMESPACE),
        instance_selector_labels=_INSTANCE_LABELS,
        datasources=[
            GrafanaDashboardSpecDatasources(input_name=input_name, datasource_name=datasource)
            for input_name, datasource in datasources.items()
        ]
        if datasources
        else None,
        config_map_ref=GrafanaDashboardSpecConfigMapRef(name=f"{name}-dashboard", key=f"{name}.json")
        if config_map
        else None,
        grafana_com=grafana_com,
    )


def _dashboards(chart: Chart) -> None:
    _dashboard(chart, "flux-cluster", config_map=True)
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
    # DCGM metrics from cluster/k8s/dcgm-exporter/ via Mimir.
    _dashboard(
        chart,
        "gpu",
        datasources={"DS_PROMETHEUS": "Mimir"},
        grafana_com=GrafanaDashboardSpecGrafanaCom(id=12239, revision=2),
    )
    _dashboard(
        chart, "interface-flap-frequency", datasources={"DS_LOKI": "Loki", "DS_PROMETHEUS": "Mimir"}, config_map=True
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
    _dashboard(chart, "rugged-power", datasources={"DS_PROMETHEUS": "Mimir"}, config_map=True)
    _dashboard(chart, "cluster-storage-io", datasources={"DS_MIMIR": "Mimir", "DS_LOKI": "Loki"}, config_map=True)


def chart(app: App) -> Chart:
    chart = Chart(app, "grafana-instance", disable_resource_name_hashes=True)
    _database(chart)
    _grafana(chart)
    _datasources(chart)
    _dashboards(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
