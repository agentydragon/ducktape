import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting

from cluster.cdk8s.providers.prometheus_operator.pod_monitor import Endpoint, PodMonitor


def test_plain_endpoint_wire_shape() -> None:
    chart = Cdk8sTesting.chart()
    PodMonitor(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-monitor"),
        selector={"app.kubernetes.io/name": "test"},
        pod_metrics_endpoints=[Endpoint.plain(port="metrics", scrape_timeout="10s")],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["selector"] == {"matchLabels": {"app.kubernetes.io/name": "test"}}
    (endpoint,) = manifest["spec"]["podMetricsEndpoints"]
    assert endpoint == {"port": "metrics", "path": "/metrics", "scrapeTimeout": "10s"}


if __name__ == "__main__":
    pytest_bazel.main()
