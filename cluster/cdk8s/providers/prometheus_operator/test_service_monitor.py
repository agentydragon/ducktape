from pathlib import Path

import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.testing.scratch import scratch_chart


def test_bearer_token_secret_endpoint_wire_shape(tmp_path: Path) -> None:
    chart = scratch_chart(tmp_path)
    ServiceMonitor(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-monitor"),
        selector=ServiceMonitorSpecSelector(match_labels={"app.kubernetes.io/name": "test"}),
        endpoints=[Endpoint.bearer_token_secret(port="http", secret_name="metrics-token", key="token")],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    (endpoint,) = manifest["spec"]["endpoints"]
    assert endpoint["bearerTokenSecret"] == {"name": "metrics-token", "key": "token"}
    assert endpoint["port"] == "http"
    assert endpoint["path"] == "/metrics"


if __name__ == "__main__":
    pytest_bazel.main()
