from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import PodMonitor, PodMonitorSpec, PodMonitorSpecSelector

from cluster.cdk8s.providers.prometheus_operator.pod_monitor import Endpoint


def test_plain_endpoint_wire_shape(scratch_chart: Chart) -> None:
    PodMonitor(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-monitor"),
        spec=PodMonitorSpec(
            selector=PodMonitorSpecSelector(match_labels={"app.kubernetes.io/name": "test"}),
            pod_metrics_endpoints=[Endpoint.plain(port="metrics", scrape_timeout="10s")],
        ),
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    (endpoint,) = manifest["spec"]["podMetricsEndpoints"]
    assert endpoint == {"port": "metrics", "path": "/metrics", "scrapeTimeout": "10s"}
