"""Grafana dashboard source-model contracts."""

import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting

from cluster.cdk8s.monitoring import grafana_instance
from cluster.cdk8s.monitoring.grafana_dashboard_inputs import FILE_DASHBOARDS


def test_home_network_dashboard_declares_operator_datasource_mapping(scratch_app: App) -> None:
    dashboards = [
        doc
        for doc in Cdk8sTesting.synth(grafana_instance.chart(scratch_app))
        if doc["kind"] == "GrafanaDashboard" and doc["metadata"]["name"] == "home-network-overview"
    ]
    assert len(dashboards) == 1
    assert dashboards[0]["spec"]["datasources"] == [{"datasourceName": "Loki", "inputName": "DS_LOKI"}]


def test_file_dashboard_source_mappings_match_synthesized_resources(scratch_app: App) -> None:
    dashboards = [
        doc
        for doc in Cdk8sTesting.synth(grafana_instance.chart(scratch_app))
        if doc["kind"] == "GrafanaDashboard" and doc["spec"].get("configMapRef")
    ]
    actual = {
        doc["spec"]["configMapRef"]["key"]: {
            item["inputName"]: item["datasourceName"] for item in doc["spec"].get("datasources", [])
        }
        for doc in dashboards
    }
    assert len(actual) == len(dashboards), "duplicate dashboard filenames"
    assert actual == {f"{name}.json": dict(dashboard.datasources) for name, dashboard in FILE_DASHBOARDS.items()}


if __name__ == "__main__":
    pytest_bazel.main()
