"""The staged remote workloads must agree without taking over the live session."""

import json
from urllib.parse import urlsplit

import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting

from cluster.cdk8s.platform_sessions import remote


def test_remote_workloads_share_connections_but_not_legacy_storage(scratch_app: App) -> None:
    objects = Cdk8sTesting.synth(remote.chart(scratch_app))

    def resource(kind, name):
        return next(item for item in objects if item["kind"] == kind and item["metadata"]["name"] == name)

    gateway = resource("Deployment", remote.GATEWAY)["spec"]
    provider = resource("Deployment", remote.BLUESHIELD)["spec"]
    for workload in (gateway, provider):
        assert workload["replicas"] == 0
        assert workload["strategy"]["type"] == "Recreate"

    provider_pod = provider["template"]["spec"]
    gateway_pod = gateway["template"]["spec"]
    assert provider_pod["nodeSelector"]["topology.kubernetes.io/zone"] == "hil-ovh"
    claims = [v["persistentVolumeClaim"]["claimName"] for v in provider_pod["volumes"] if "persistentVolumeClaim" in v]
    assert claims == [remote.BLUESHIELD]
    assert all("persistentVolumeClaim" not in volume for volume in gateway_pod["volumes"])

    assert remote.CONFIG.data is not None
    connection = json.loads(remote.CONFIG.data["connections.json"])["blueshield"]
    host = urlsplit(connection["origin"]).hostname
    service = resource("Service", remote.BLUESHIELD)["spec"]
    assert service["selector"] == provider["template"]["metadata"]["labels"]
    assert {p["name"]: p["port"] for p in service["ports"]} == {"https": 8443, "grpc": 8444}
    assert connection["control_target"] == f"{host}:8444"
    certificate = resource("Certificate", remote.BLUESHIELD)["spec"]
    assert host in certificate["dnsNames"]

    monitor = resource("PodMonitor", remote.BLUESHIELD)["spec"]
    assert monitor["selector"]["matchLabels"] == service["selector"]
    assert monitor["podMetricsEndpoints"][0]["port"] == "metrics"
    ports = {p["name"] for p in provider_pod["containers"][0]["ports"]}
    assert "metrics" in ports


if __name__ == "__main__":
    pytest_bazel.main()
