"""CDviz's PostgreSQL datasource and CI workflow dashboard in Grafana."""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_grafanadashboard_crds.org.integreatly.grafana import GrafanaDashboardSpecInstanceSelector
from grafana_grafanadatasource_crds.org.integreatly.grafana import (
    GrafanaDatasourceSpecDatasource,
    GrafanaDatasourceSpecInstanceSelector,
    GrafanaDatasourceSpecValuesFrom,
    GrafanaDatasourceSpecValuesFromValueFrom,
    GrafanaDatasourceSpecValuesFromValueFromSecretKeyRef,
)

from cluster.cdk8s.cdviz import config, dashboard, feedback_dashboard
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.monitoring import grafana_instance
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef
from cluster.cdk8s.providers.grafana_operator.grafana_dashboard import GrafanaDashboard
from cluster.cdk8s.providers.grafana_operator.grafana_datasource import GrafanaDatasource

_NAMESPACE = grafana_instance.SERVICE.pods.namespace
_PASSWORD = "cdviz-database"


def chart(app: App) -> Chart:
    chart = Chart(app, "cdviz-dashboard", disable_resource_name_hashes=True)
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
        instance_selector=GrafanaDatasourceSpecInstanceSelector(match_labels=grafana_instance.INSTANCE_LABELS),
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
    for name, definition in (
        ("cdviz-ci", dashboard.build_dashboard()),
        ("cdviz-ci-feedback", feedback_dashboard.build_dashboard()),
    ):
        GrafanaDashboard(
            chart,
            name,
            metadata=ApiObjectMetadata(name=name, namespace=_NAMESPACE),
            instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels=grafana_instance.INSTANCE_LABELS),
            folder="CI",
            json=JSONEncoder().encode(definition),
        )
    return chart
