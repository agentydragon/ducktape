"""Grafana dashboard source-model contracts."""

import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting

from cluster.cdk8s.monitoring import grafana_instance


def test_home_network_dashboard_declares_operator_datasource_mapping(scratch_app: App) -> None:
    dashboards = [
        doc
        for doc in Cdk8sTesting.synth(grafana_instance.chart(scratch_app))
        if doc["kind"] == "GrafanaDashboard" and doc["metadata"]["name"] == "home-network-overview"
    ]
    assert len(dashboards) == 1
    assert dashboards[0]["spec"]["datasources"] == [{"datasourceName": "Loki", "inputName": "DS_LOKI"}]


if __name__ == "__main__":
    pytest_bazel.main()
