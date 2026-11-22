"""The CPAP guest exposes the card to the sync job and filesystem metrics to Alloy."""

import pytest_bazel
from cdk8s import App

from cluster.cdk8s import cpap_sync


def test_gateway_listeners_and_ingress(scratch_app: App) -> None:
    chart = cpap_sync.chart(scratch_app)
    objects = {(obj.kind, obj.name): obj for obj in chart.api_objects}
    guest = objects["VirtualMachine", "cpap-gateway"].to_json()["spec"]["template"]
    (interface,) = guest["spec"]["domain"]["devices"]["interfaces"]
    listeners = {port["name"]: port["port"] for port in interface["ports"]}
    card = objects["Service", "cpap-card"].to_json()
    metrics = objects["Service", "cpap-gateway-metrics"].to_json()
    # The NixOS appliance runs socat on 18080 and node_exporter on 9100.
    for service, guest_port in ((card, 18080), (metrics, 9100)):
        selector = service["spec"]["selector"]
        assert selector
        assert selector.items() <= guest["metadata"]["labels"].items()
        (port,) = service["spec"]["ports"]
        assert port["targetPort"] == listeners[port["name"]] == guest_port
    assert len(listeners) == 2

    monitor = objects["ServiceMonitor", "cpap-gateway-metrics"].to_json()["spec"]
    assert monitor["selector"]["matchLabels"] == metrics["metadata"]["labels"]
    assert monitor["endpoints"] == [{"port": metrics["spec"]["ports"][0]["name"], "path": "/metrics"}]

    policy = objects["CiliumNetworkPolicy", "guest-metrics-ingress"].to_json()["spec"]
    assert policy["endpointSelector"]["matchLabels"] == metrics["spec"]["selector"]
    job = objects["CronJob", "cpap-sync"].to_json()
    sync_labels = job["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    expected = [
        {"fromEndpoints": [{"matchLabels": labels}], "toPorts": [{"ports": [{"port": port, "protocol": "TCP"}]}]}
        for port, labels in (
            ("18080", {**sync_labels, "k8s:io.kubernetes.pod.namespace": job["metadata"]["namespace"]}),
            ("9100", {"app.kubernetes.io/name": "alloy-node", "k8s:io.kubernetes.pod.namespace": "monitoring"}),
        )
    ]
    assert len(policy["ingress"]) == len(expected)
    assert all(rule in policy["ingress"] for rule in expected)
    assert "egress" not in policy


if __name__ == "__main__":
    pytest_bazel.main()
