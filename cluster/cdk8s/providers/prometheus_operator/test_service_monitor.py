import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting

from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor


def test_bearer_token_secret_endpoint_wire_shape() -> None:
    chart = Cdk8sTesting.chart()
    ServiceMonitor(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-monitor"),
        selector={"app.kubernetes.io/name": "test"},
        endpoints=[Endpoint.bearer_token_secret(port="http", secret_name="metrics-token", key="token")],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    (endpoint,) = manifest["spec"]["endpoints"]
    assert endpoint["bearerTokenSecret"] == {"name": "metrics-token", "key": "token"}
    assert endpoint["port"] == "http"
    assert endpoint["path"] == "/metrics"


def test_namespace_selector_widens_discovery_beyond_own_namespace() -> None:
    chart = Cdk8sTesting.chart()
    ServiceMonitor(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-monitor"),
        selector={"app.kubernetes.io/name": "test"},
        endpoints=[Endpoint.plain(port="http")],
        namespace_selector=["kube-system"],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["namespaceSelector"] == {"matchNames": ["kube-system"]}


if __name__ == "__main__":
    pytest_bazel.main()
