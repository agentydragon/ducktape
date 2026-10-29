from pathlib import Path

import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import PodMonitorSpecSelector

from cluster.cdk8s.providers.prometheus_operator.pod_monitor import Endpoint, PodMonitor
from cluster.cdk8s.testing.scratch import scratch_chart


def test_plain_endpoint_wire_shape(tmp_path: Path) -> None:
    chart = scratch_chart(tmp_path)
    PodMonitor(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-monitor"),
        selector=PodMonitorSpecSelector(match_labels={"app.kubernetes.io/name": "test"}),
        pod_metrics_endpoints=[Endpoint.plain(port="metrics", scrape_timeout="10s")],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    (endpoint,) = manifest["spec"]["podMetricsEndpoints"]
    assert endpoint == {"port": "metrics", "path": "/metrics", "scrapeTimeout": "10s"}


if __name__ == "__main__":
    pytest_bazel.main()
