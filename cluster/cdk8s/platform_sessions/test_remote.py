"""The gateway and provider workloads agree on their connections and storage."""

import json
from urllib.parse import urlsplit

import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting

from cluster.cdk8s.platform_sessions import app, config, remote


def test_remote_workloads_share_connections_but_not_legacy_storage(scratch_app: App) -> None:
    objects = Cdk8sTesting.synth(remote.chart(scratch_app, record_upstream_traffic=False))

    def resource(kind, name):
        return next(item for item in objects if item["kind"] == kind and item["metadata"]["name"] == name)

    gateway = resource("Deployment", remote.GATEWAY)["spec"]
    assert gateway["strategy"]["type"] == "Recreate"
    gateway_pod = gateway["template"]["spec"]
    assert all("persistentVolumeClaim" not in volume for volume in gateway_pod["volumes"])
    public_objects = Cdk8sTesting.synth(app.chart(scratch_app))
    public_service = next(
        obj for obj in public_objects if obj["kind"] == "Service" and obj["metadata"]["name"] == app.NAME
    )
    assert public_service["spec"]["selector"] == gateway["template"]["metadata"]["labels"]

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
        assert provider["strategy"]["type"] == "Recreate"
        provider_pod = provider["template"]["spec"]
        expected_image = remote._provider_image(instance)
        assert provider_pod["containers"][0]["image"] == f"{remote.REGISTRY}/{expected_image}:unset"
        assert provider_pod["nodeSelector"]["topology.kubernetes.io/zone"] == "hil-ovh"
        claims = [
            v["persistentVolumeClaim"]["claimName"] for v in provider_pod["volumes"] if "persistentVolumeClaim" in v
        ]
        if instance in remote.PIRAEUS_MIGRATIONS:
            assert claims == [f"{name}-piraeus", name]
            claim = resource("PersistentVolumeClaim", f"{name}-piraeus")
            assert claim["spec"]["storageClassName"] == "piraeus-ssd-2-autodiskful"
            assert claim["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
            old = next(volume for volume in provider_pod["volumes"] if volume["name"] == "old-data")
            assert old["persistentVolumeClaim"]["readOnly"]
            mount = next(mount for mount in provider_pod["containers"][0]["volumeMounts"] if mount["name"] == "data")
            assert mount["subPath"] == "state"
        else:
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


def test_recording_wires_each_provider_to_its_private_hdd_capture(scratch_app: App) -> None:
    objects = Cdk8sTesting.synth(remote.chart(scratch_app, record_upstream_traffic=True))

    def resource(kind, name):
        return next(item for item in objects if item["kind"] == kind and item["metadata"]["name"] == name)

    assert config.RECORD_UPSTREAM_TRAFFIC
    for instance in remote.PROVIDERS:
        name = f"platform-sessions-{instance}"
        pod = resource("Deployment", name)["spec"]["template"]["spec"]
        containers = {container["name"]: container for container in pod["containers"]}
        assert set(containers) == {"provider"}
        init = pod["initContainers"]
        assert [container["name"] for container in init] == (
            (["migrate-state"] if instance in remote.PIRAEUS_MIGRATIONS else [])
            + ["recorder-ca-init", "upstream-recorder"]
        )
        recorder = init[-1]
        assert recorder["restartPolicy"] == "Always"
        assert "startupProbe" in recorder
        env = {item["name"]: item["value"] for item in containers["provider"]["env"]}
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:18080"
        assert env["SSL_CERT_FILE"] == "/var/run/session-recorder-trust/ca-certificates.crt"
        assert env["PLATFORM_SESSIONS_PROXY_ENABLED"] == "true"
        assert env["PLATFORM_SESSIONS_PROXY_CA_FILE"] == "/var/run/session-recorder-ca/tls.crt"
        assert "+/captures/%Y-%m-%d.mitm" in recorder["command"][-1]
        claim = resource("PersistentVolumeClaim", f"{name}-captures")
        assert claim["spec"]["storageClassName"] == "piraeus-hdd-local"
        assert claim["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
        capture = next(volume for volume in pod["volumes"] if volume["name"] == "captures")
        assert capture["persistentVolumeClaim"]["claimName"] == f"{name}-captures"
        assert not any(mount["name"] == "captures" for mount in containers["provider"]["volumeMounts"])


def test_disabled_recording_keeps_capture_claims_without_proxy(scratch_app: App) -> None:
    objects = Cdk8sTesting.synth(remote.chart(scratch_app, record_upstream_traffic=False))
    for instance in remote.PROVIDERS:
        name = f"platform-sessions-{instance}"
        pod = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == name)[
            "spec"
        ]["template"]["spec"]
        assert [item["name"] for item in pod["containers"]] == ["provider"]
        assert not any(item["name"] == "HTTPS_PROXY" for item in pod["containers"][0]["env"])
        assert any(
            item["kind"] == "PersistentVolumeClaim" and item["metadata"]["name"] == f"{name}-captures"
            for item in objects
        )


if __name__ == "__main__":
    pytest_bazel.main()
