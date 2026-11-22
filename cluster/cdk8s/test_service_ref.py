"""Service references distinguish client-facing ports from Pod ports and preserve endpoint overrides."""

import pytest_bazel
from more_itertools import one
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecEndpoints

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_PODS = Pods(namespace="test-namespace", labels=(("app.kubernetes.io/name", "test-app"),))
# A Helm chart's shape: Service port 80 in front of Pods listening on 8080.
_SERVICE = ServiceRef(name="test-app", port=Port(name="http", number=80), pods=_PODS, target_port=8080)


def test_client_addresses_dial_the_service_port() -> None:
    assert _SERVICE.url == "http://test-app.test-namespace.svc:80"
    assert _SERVICE.fqdn == "test-app.test-namespace.svc.cluster.local"


def test_cilium_rules_name_the_pods_port() -> None:
    ingress = Pods(namespace="test-client", labels=()).admit(_SERVICE.pod_port)
    # No labels admits the whole namespace.
    assert one(ingress.from_endpoints or []).match_labels == {"k8s:io.kubernetes.pod.namespace": "test-client"}
    assert one(one(ingress.to_ports or []).ports or []).port == "8080"
    egress = _SERVICE.egress()
    assert one(egress.to_endpoints or []).match_labels == {
        "k8s:io.kubernetes.pod.namespace": "test-namespace",
        "app.kubernetes.io/name": "test-app",
    }
    assert one(one(egress.to_ports or []).ports or []).port == "8080"


def test_service_port_targets_the_pods_port() -> None:
    port = _SERVICE.service_port()
    assert port.port == 80
    assert port.target_port == 8080
    assert _SERVICE.container_port().number == 8080


def test_monitor_derives_service_selector_and_port_without_changing_custom_endpoints() -> None:
    default = _SERVICE.service_monitor_spec()
    assert default.selector.match_labels == {"app.kubernetes.io/name": "test-app"}
    endpoint = one(default.endpoints)
    assert endpoint.port == "http"
    assert endpoint.path == "/metrics"
    custom = ServiceMonitorSpecEndpoints(port="other-port", path="/custom-metrics", interval="37s")
    assert _SERVICE.service_monitor_spec(custom).endpoints == [custom]


if __name__ == "__main__":
    pytest_bazel.main()
