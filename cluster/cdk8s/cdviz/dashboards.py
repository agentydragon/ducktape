"""CDviz's PostgreSQL datasource and upstream execution dashboards in Grafana."""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy
from grafana_grafanadashboard_crds.org.integreatly.grafana import GrafanaDashboardSpecInstanceSelector
from grafana_grafanadatasource_crds.org.integreatly.grafana import (
    GrafanaDatasourceSpecDatasource,
    GrafanaDatasourceSpecInstanceSelector,
    GrafanaDatasourceSpecValuesFrom,
    GrafanaDatasourceSpecValuesFromValueFrom,
    GrafanaDatasourceSpecValuesFromValueFromSecretKeyRef,
)

from cluster.cdk8s.cdviz import config
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.grafana_dashboards import DashboardFile
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef
from cluster.cdk8s.providers.grafana_operator.grafana_dashboard import GrafanaDashboard
from cluster.cdk8s.providers.grafana_operator.grafana_datasource import GrafanaDatasource

_NAMESPACE = "monitoring"
_PASSWORD = "cdviz-database"
_INSTANCE = {"dashboards": "grafana"}
DASHBOARDS = [
    DashboardFile(source=f"cluster/cdk8s/cdviz/{name}.json", config_map=f"cdviz-{name}", namespace=_NAMESPACE)
    for name in ("pipelines", "tasks")
]


def chart(app: App) -> Chart:
    chart = Chart(app, "cdviz-dashboards", disable_resource_name_hashes=True)
    reader = ServiceAccount(
        chart, "reader", metadata=ApiObjectMetadata(name="cdviz-database-reader", namespace=_NAMESPACE)
    )
    store = single_secret_store(
        chart,
        "cdviz-database",
        reader=reader,
        source_namespace=config.NAMESPACE,
        source_secret=config.GRAFANA_PASSWORD.name,
        consumer_namespace=_NAMESPACE,
    )
    ExternalSecret(
        chart,
        "password",
        metadata=ApiObjectMetadata(name=_PASSWORD, namespace=_NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(store),
        data_from=[DataFrom.from_extract(config.GRAFANA_PASSWORD.name)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    GrafanaDatasource(
        chart,
        "datasource",
        metadata=ApiObjectMetadata(name="cdviz", namespace=_NAMESPACE),
        instance_selector=GrafanaDatasourceSpecInstanceSelector(match_labels=_INSTANCE),
        datasource=GrafanaDatasourceSpecDatasource(
            name="CI Analytics",
            uid="cdviz",
            type="grafana-postgresql-datasource",
            access="proxy",
            url=f"{config.DATABASE.rw.host}:{config.DATABASE.rw.port.number}",
            user="cdviz_reader",
            is_default=False,
            editable=False,
            json_data={
                "database": config.DATABASE_NAME,
                "sslmode": "require",
                "postgresVersion": 1800,
                "timescaledb": True,
            },
            secure_json_data={"password": "${password}"},
        ),
        values_from=[
            GrafanaDatasourceSpecValuesFrom(
                target_path="secureJsonData.password",
                value_from=GrafanaDatasourceSpecValuesFromValueFrom(
                    secret_key_ref=GrafanaDatasourceSpecValuesFromValueFromSecretKeyRef(name=_PASSWORD, key="password")
                ),
            )
        ],
    )
    for dashboard in DASHBOARDS:
        GrafanaDashboard(
            chart,
            dashboard.config_map,
            metadata=ApiObjectMetadata(name=dashboard.config_map, namespace=_NAMESPACE),
            instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels=_INSTANCE),
            folder="CI",
            config_map_ref=dashboard.config_map_ref(),
        )
    return chart
