"""AIQuota's ClickHouse datasource and history dashboard (`dashboard.json` beside this
module) in the monitoring Grafana."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from grafana_grafanadashboard_crds.org.integreatly.grafana import GrafanaDashboardSpecInstanceSelector
from grafana_grafanadatasource_crds.org.integreatly.grafana import (
    GrafanaDatasourceSpecDatasource,
    GrafanaDatasourceSpecInstanceSelector,
    GrafanaDatasourceSpecValuesFrom,
    GrafanaDatasourceSpecValuesFromValueFrom,
    GrafanaDatasourceSpecValuesFromValueFromSecretKeyRef,
)

from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.grafana_dashboards import DashboardFile
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.grafana_operator.grafana_dashboard import GrafanaDashboard
from cluster.cdk8s.providers.grafana_operator.grafana_datasource import GrafanaDatasource

OUTPUT_DIR = f"{GENERATED_ROOT}/grafana"
_NAMESPACE = "monitoring"
_INSTANCE_LABELS = {"dashboards": "grafana"}
DASHBOARD = DashboardFile(
    source="cluster/cdk8s/grafana/dashboard.json", config_map="aiquota-history-dashboard", namespace=_NAMESPACE
)


def chart(app: App) -> Chart:
    chart = Chart(app, "clickhouse-grafana", disable_resource_name_hashes=True)
    GrafanaDatasource(
        chart,
        "datasource",
        metadata=ApiObjectMetadata(name="clickhouse", namespace=_NAMESPACE),
        instance_selector=GrafanaDatasourceSpecInstanceSelector(match_labels=_INSTANCE_LABELS),
        values_from=[
            GrafanaDatasourceSpecValuesFrom(
                target_path="secureJsonData.password",
                value_from=GrafanaDatasourceSpecValuesFromValueFrom(
                    secret_key_ref=GrafanaDatasourceSpecValuesFromValueFromSecretKeyRef(
                        name="clickhouse-grafana-credentials", key="password"
                    )
                ),
            )
        ],
        datasource=GrafanaDatasourceSpecDatasource(
            name="AIQuota Analytics",
            uid="clickhouse",
            type="grafana-clickhouse-datasource",
            access="proxy",
            is_default=False,
            editable=False,
            json_data={
                "host": client.HTTP.host,
                "port": client.HTTP.port.number,
                "protocol": "http",
                "username": "grafana",
                "defaultDatabase": "aiquota",
                "secure": False,
                "validateSql": True,
                "queryTimeout": 60,
            },
            secure_json_data={"password": "${password}"},
        ),
    )
    GrafanaDashboard(
        chart,
        "dashboard",
        metadata=ApiObjectMetadata(name="aiquota-history", namespace=_NAMESPACE),
        instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels=_INSTANCE_LABELS),
        folder="Analytics",
        config_map_ref=DASHBOARD.config_map_ref(),
    )
    return chart


def clickhouse_grafana(
    flux_chart: Chart, directory: RenderedDirectory, grafana_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "clickhouse-grafana",
        directory,
        timeout="5m",
        # The GrafanaDashboard and GrafanaDatasource CRDs.
        depends_on=flux_kustomization_depends_on_many(grafana_operator),
    )
