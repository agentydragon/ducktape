"""AIQuota's ClickHouse datasource and history dashboard in the monitoring Grafana.

Hand-written beside the generated output: `dashboard.json` (rendered into the
`aiquota-history-dashboard` ConfigMap by the directory's `configMapGenerator`),
`kustomizeconfig` (which points the dashboard's `configMapRef` at the generated,
hash-suffixed ConfigMap name) and the `kustomization.yaml` that wires both.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from grafana_grafanadashboard_crds.org.integreatly.grafana import (
    GrafanaDashboard,
    GrafanaDashboardSpec,
    GrafanaDashboardSpecConfigMapRef,
    GrafanaDashboardSpecInstanceSelector,
)
from grafana_grafanadatasource_crds.org.integreatly.grafana import (
    GrafanaDatasource,
    GrafanaDatasourceSpec,
    GrafanaDatasourceSpecDatasource,
    GrafanaDatasourceSpecInstanceSelector,
    GrafanaDatasourceSpecValuesFrom,
    GrafanaDatasourceSpecValuesFromValueFrom,
    GrafanaDatasourceSpecValuesFromValueFromSecretKeyRef,
)

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

_OUTPUT_DIR = "cluster/k8s/grafana"
_NAMESPACE = "monitoring"
_INSTANCE_LABELS = {"dashboards": "grafana"}


def chart(app: App) -> Chart:
    chart = Chart(app, "clickhouse-grafana", disable_resource_name_hashes=True)
    GrafanaDatasource(
        chart,
        "datasource",
        metadata=metadata("clickhouse", _NAMESPACE),
        spec=GrafanaDatasourceSpec(
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
                    "host": "clickhouse.clickhouse.svc.cluster.local",
                    "port": 8123,
                    "protocol": "http",
                    "username": "grafana",
                    "defaultDatabase": "aiquota",
                    "secure": False,
                    "validateSql": True,
                    "queryTimeout": 60,
                },
                secure_json_data={"password": "${password}"},
            ),
        ),
    )
    GrafanaDashboard(
        chart,
        "dashboard",
        metadata=metadata("aiquota-history", _NAMESPACE),
        spec=GrafanaDashboardSpec(
            folder="Analytics",
            instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels=_INSTANCE_LABELS),
            config_map_ref=GrafanaDashboardSpecConfigMapRef(name="aiquota-history-dashboard", key="dashboard.json"),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
