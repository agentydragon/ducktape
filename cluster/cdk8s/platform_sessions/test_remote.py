"""The staged gateway and providers preserve the running legacy service."""

import json
from urllib.parse import urlsplit

import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting

from cluster.cdk8s.platform_sessions import app, config, remote


def test_remote_workloads_share_connections_but_not_legacy_storage(scratch_app: App) -> None:
    objects = Cdk8sTesting.synth(remote.chart(scratch_app))

    def resource(kind, name):
        return next(item for item in objects if item["kind"] == kind and item["metadata"]["name"] == name)

    gateway = resource("Deployment", remote.GATEWAY)["spec"]
    assert gateway["replicas"] == 0
    assert gateway["strategy"]["type"] == "Recreate"
    gateway_pod = gateway["template"]["spec"]
    assert all("persistentVolumeClaim" not in volume for volume in gateway_pod["volumes"])
    public_objects = Cdk8sTesting.synth(app.chart(scratch_app))
    public_service = next(
        obj for obj in public_objects if obj["kind"] == "Service" and obj["metadata"]["name"] == app.NAME
    )
    assert public_service["spec"]["selector"] == {"app": app.NAME}
    legacy = next(obj for obj in public_objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == app.NAME)[
        "spec"
    ]
    assert legacy["replicas"] == 1
    assert legacy["template"]["spec"]["containers"][0]["image"] == app.IMAGE
    legacy_monitor = next(
        obj
        for obj in public_objects
        if obj["kind"] == "PodMonitor" and obj["metadata"]["name"] == "platform-sessions-quest"
    )
    assert legacy_monitor["spec"]["selector"]["matchLabels"] == {"app": app.NAME}

    assert remote.CONFIG.data is not None
    connections = json.loads(remote.CONFIG.data["connections.json"])
    gateway_integrations = json.loads(remote.CONFIG.data["gateway-integrations.json"])["integrations"]
    assert set(connections) == {item["id"] for item in config.INTEGRATIONS["integrations"]}
    assert set(connections) == {item["id"] for item in gateway_integrations}
    assert json.loads(remote.CONFIG.data["bearers.json"]) == config.BEARERS
    assert {
        resource("Deployment", f"platform-sessions-{instance}")["spec"]["template"]["spec"]["containers"][0]["image"]
        for instance in ("openai", "anthropic", "thrive")
    } == {f"{remote.REGISTRY}/{remote.SHARED_PROVIDERS_IMAGE}:unset"}
    assert (
        sum(
            item["kind"] == "ImagePolicy" and item["metadata"]["name"] == remote.SHARED_PROVIDERS_IMAGE
            for item in objects
        )
        == 1
    )
    for instance, connection in connections.items():
        name = f"platform-sessions-{instance}"
        provider = resource("Deployment", name)["spec"]
        assert provider["replicas"] == 0
        assert provider["strategy"]["type"] == "Recreate"
        provider_pod = provider["template"]["spec"]
        expected_image = remote._provider_image(instance)
        assert provider_pod["containers"][0]["image"] == f"{remote.REGISTRY}/{expected_image}:unset"
        assert provider_pod["nodeSelector"]["topology.kubernetes.io/zone"] == "hil-ovh"
        claims = [
            v["persistentVolumeClaim"]["claimName"] for v in provider_pod["volumes"] if "persistentVolumeClaim" in v
        ]
        assert claims == [name]
        assert json.loads(remote.CONFIG.data[f"{instance}-integration.json"])["integrations"][0]["id"] == instance

        host = urlsplit(connection["origin"]).hostname
        service = resource("Service", name)["spec"]
        assert service["selector"] == provider["template"]["metadata"]["labels"]
        assert {p["name"]: p["port"] for p in service["ports"]} == {"https": 8443, "grpc": 8444}
        assert connection["control_target"] == f"{host}:8444"
        assert host in resource("Certificate", name)["spec"]["dnsNames"]
        monitor_name = f"{name}-provider" if instance == "quest" else name
        monitor = resource("PodMonitor", monitor_name)["spec"]
        assert monitor["selector"]["matchLabels"] == service["selector"]
        assert monitor["podMetricsEndpoints"][0]["port"] == "metrics"
        if instance == "quest":
            assert monitor["selector"]["matchLabels"] == {"app": "platform-sessions-quest"}
            assert {endpoint["port"] for endpoint in monitor["podMetricsEndpoints"]} == {"metrics", "quest-metrics"}
        assert "metrics" in {p["name"] for p in provider_pod["containers"][0]["ports"]}


if __name__ == "__main__":
    pytest_bazel.main()
