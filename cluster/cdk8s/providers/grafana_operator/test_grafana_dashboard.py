import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from grafana_grafanadashboard_crds.org.integreatly.grafana import (
    GrafanaDashboardSpecConfigMapRef,
    GrafanaDashboardSpecDatasources,
    GrafanaDashboardSpecGrafanaCom,
)

from cluster.cdk8s.providers.grafana_operator.grafana_dashboard import GrafanaDashboard


def test_instance_selector_labels_become_nested_selector() -> None:
    chart = Cdk8sTesting.chart()
    GrafanaDashboard(
        chart, "test", metadata=ApiObjectMetadata(name="dashboard"), instance_selector_labels={"dashboards": "grafana"}
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    spec = manifest["spec"]
    assert spec["instanceSelector"] == {"matchLabels": {"dashboards": "grafana"}}
    assert "folder" not in spec
    assert "configMapRef" not in spec


def test_optional_fields_pass_through() -> None:
    chart = Cdk8sTesting.chart()
    GrafanaDashboard(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="dashboard"),
        instance_selector_labels={"dashboards": "grafana"},
        folder="Analytics",
        datasources=[GrafanaDashboardSpecDatasources(input_name="DS_PROMETHEUS", datasource_name="Mimir")],
        config_map_ref=GrafanaDashboardSpecConfigMapRef(name="dashboard-cm", key="dashboard.json"),
        grafana_com=GrafanaDashboardSpecGrafanaCom(id=1234, revision=5),
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    spec = manifest["spec"]
    assert spec["folder"] == "Analytics"
    assert spec["datasources"] == [{"inputName": "DS_PROMETHEUS", "datasourceName": "Mimir"}]
    assert spec["configMapRef"] == {"name": "dashboard-cm", "key": "dashboard.json"}
    assert spec["grafanaCom"] == {"id": 1234, "revision": 5}


if __name__ == "__main__":
    pytest_bazel.main()
