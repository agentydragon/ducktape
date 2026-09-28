"""What a Service reference derives: the address a client dials names the Service port, while the
Cilium rules on both ends name the Pods' port, which Cilium matches after socket-LB translation;
and a `Port` renders one agreeing container port and Service port in both builder tiers."""

import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from cdk8s_plus_34 import Deployment, Service, k8s

from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_PODS = Pods(namespace="test-namespace", labels=(("app.kubernetes.io/name", "test-app"),))
# A Helm chart's shape: Service port 80 in front of Pods listening on 8080.
_SERVICE = ServiceRef(name="test-app", port=Port(name="http", number=80), pods=_PODS, target_port=8080)


@pytest.fixture
def chart() -> Chart:
    return Cdk8sTesting.chart()


def test_client_addresses_dial_the_service_port() -> None:
    assert _SERVICE.url == "http://test-app.test-namespace.svc:80"
    assert _SERVICE.fqdn == "test-app.test-namespace.svc.cluster.local"


def test_cilium_rules_name_the_pods_port(chart: Chart) -> None:
    client = Pods(namespace="test-client", labels=())
    NetworkPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(name="test-policy", namespace="test-namespace"),
        endpoint_selector=_PODS.selector,
        ingress=[client.admit(_SERVICE.pod_port)],
        egress=[_SERVICE.egress()],
    )
    [policy] = Cdk8sTesting.synth(chart)
    tcp_8080 = [{"ports": [{"port": "8080", "protocol": "TCP"}]}]
    # No labels admits the whole namespace.
    assert policy["spec"]["ingress"] == [
        {"fromEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "test-client"}}], "toPorts": tcp_8080}
    ]
    assert policy["spec"]["egress"] == [
        {
            "toEndpoints": [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "test-namespace",
                        "app.kubernetes.io/name": "test-app",
                    }
                }
            ],
            "toPorts": tcp_8080,
        }
    ]


def test_port_renders_one_container_and_service_port_in_both_tiers(chart: Chart) -> None:
    port = Port(name="http", number=8080)
    deployment = Deployment(chart, "deployment", metadata=ApiObjectMetadata(name="test-app"))
    deployment.add_container(image="app.test.invalid/app", ports=[port.container_port()])
    Service(
        chart, "service", metadata=ApiObjectMetadata(name="test-app"), selector=deployment, ports=[port.service_port()]
    )
    k8s.KubePod(
        chart,
        "raw-pod",
        metadata=k8s.ObjectMeta(name="test-raw"),
        spec=k8s.PodSpec(
            containers=[k8s.Container(name="app", image="app.test.invalid/app", ports=[port.k8s_container_port()])]
        ),
    )
    k8s.KubeService(
        chart,
        "raw-service",
        metadata=k8s.ObjectMeta(name="test-raw"),
        spec=k8s.ServiceSpec(ports=[port.k8s_service_port()]),
    )
    rendered, service, raw_pod, raw_service = Cdk8sTesting.synth(chart)
    container_port = {"name": "http", "containerPort": 8080, "protocol": "TCP"}
    service_port = {"name": "http", "port": 8080, "targetPort": 8080, "protocol": "TCP"}
    assert rendered["spec"]["template"]["spec"]["containers"][0]["ports"] == [container_port]
    assert raw_pod["spec"]["containers"][0]["ports"] == [container_port]
    assert service["spec"]["ports"] == [service_port]
    assert raw_service["spec"]["ports"] == [service_port]


if __name__ == "__main__":
    pytest_bazel.main()
