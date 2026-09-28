import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from grafana_grafanadatasource_crds.org.integreatly.grafana import (
    GrafanaDatasourceSpecDatasource,
    GrafanaDatasourceSpecInstanceSelector,
    GrafanaDatasourceSpecValuesFrom,
    GrafanaDatasourceSpecValuesFromValueFrom,
    GrafanaDatasourceSpecValuesFromValueFromSecretKeyRef,
)

from cluster.cdk8s.providers.grafana_operator.grafana_datasource import GrafanaDatasource


def test_values_from_passes_through() -> None:
    chart = Cdk8sTesting.chart()
    GrafanaDatasource(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="datasource"),
        instance_selector=GrafanaDatasourceSpecInstanceSelector(match_labels={"dashboards": "grafana"}),
        datasource=GrafanaDatasourceSpecDatasource(
            name="AIQuota", type="grafana-clickhouse-datasource", access="proxy"
        ),
        values_from=[
            GrafanaDatasourceSpecValuesFrom(
                target_path="secureJsonData.password",
                value_from=GrafanaDatasourceSpecValuesFromValueFrom(
                    secret_key_ref=GrafanaDatasourceSpecValuesFromValueFromSecretKeyRef(
                        name="clickhouse-credentials", key="password"
                    )
                ),
            )
        ],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["valuesFrom"] == [
        {
            "targetPath": "secureJsonData.password",
            "valueFrom": {"secretKeyRef": {"name": "clickhouse-credentials", "key": "password"}},
        }
    ]


if __name__ == "__main__":
    pytest_bazel.main()
