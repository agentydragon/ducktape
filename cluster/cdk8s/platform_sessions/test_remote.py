"""The gateway and provider workloads agree on their connections and storage."""

import json
from urllib.parse import urlsplit

import pytest_bazel
from cdk8s import App, Chart, Testing as Cdk8sTesting

from cluster.cdk8s.platform_sessions import app, config, remote


def test_remote_workloads_share_connections_but_not_legacy_storage(scratch_app: App) -> None:
    objects = Cdk8sTesting.synth(remote.chart(scratch_app, record_upstream_traffic=False))

    def resource(kind, name):
        return next(item for item in objects if item["kind"] == kind and item["metadata"]["name"] == name)

    gateway = resource("Deployment", "gateway")["spec"]
    assert gateway["strategy"]["type"] == "Recreate"
    gateway_pod = gateway["template"]["spec"]
    gateway_container = gateway_pod["containers"][0]
    assert {port["name"]: port["containerPort"] for port in gateway_container["ports"]} == {"http": 8080}
    assert all(not (item["kind"] == "Certificate" and item["metadata"]["name"] == "gateway") for item in objects)
    assert all(not env["name"].startswith("PLATFORM_SESSIONS_GRPC_") for env in gateway_container["env"])
    gateway_service = resource("Service", "gateway")["spec"]
    assert {port["name"]: port["port"] for port in gateway_service["ports"]} == {"http": 8080}
    assert all("persistentVolumeClaim" not in volume for volume in gateway_pod["volumes"])
    public_objects = Cdk8sTesting.synth(app.chart(scratch_app))
    public_service = next(
        obj for obj in public_objects if obj["kind"] == "Service" and obj["metadata"]["name"] == app.NAME
    )
    assert public_service["spec"]["selector"] == gateway["template"]["metadata"]["labels"]

    assert remote.CONFIG.data is not None
    assert remote.CANARY_CONFIG.data is not None
    connections = json.loads(remote.CANARY_CONFIG.data["connections.json"])
    gateway_integrations = json.loads(remote.CANARY_CONFIG.data["gateway-integrations.json"])["integrations"]
    assert set(connections) == {item["id"] for item in config.INTEGRATIONS["integrations"]}
    assert set(connections) == {item["id"] for item in gateway_integrations}
    assert json.loads(remote.CONFIG.data["bearers.json"]) == config.BEARERS
    assert "myquest-canary" not in json.loads(remote.CONFIG.data["connections.json"])
    assert {
        item["valueFrom"]["configMapKeyRef"]["name"]
        for item in gateway_container["env"]
        if item["name"] in {"PLATFORM_SESSIONS_INTEGRATION_CONFIG", "PLATFORM_SESSIONS_PROVIDER_CONNECTIONS"}
    } == {remote.CANARY_CONFIG.name}
    assert {
        resource("Deployment", instance)["spec"]["template"]["spec"]["containers"][0]["image"]
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
        name = remote._provider_name(instance)
        provider = resource("Deployment", instance)["spec"]
        assert provider["strategy"]["type"] == "Recreate"
        provider_pod = provider["template"]["spec"]
        assert next(volume for volume in provider_pod["volumes"] if volume["name"] == "config")["configMap"][
            "name"
        ] == (remote.CANARY_CONFIG.name if instance == "myquest-canary" else remote.CONFIG.name)
        expected_image = remote._provider_image(instance)
        assert provider_pod["containers"][0]["image"] == f"{remote.REGISTRY}/{expected_image}:unset"
        args = provider_pod["containers"][0]["args"]
        assert ("--route-grants" in args) == (instance not in {"blueshield", "myquest", "myquest-canary"})
        assert provider_pod["nodeSelector"]["topology.kubernetes.io/zone"] == "hil-ovh"
        claims = [
            v["persistentVolumeClaim"]["claimName"] for v in provider_pod["volumes"] if "persistentVolumeClaim" in v
        ]
        assert claims == [f"{name}-piraeus"]
        claim = resource("PersistentVolumeClaim", f"{name}-piraeus")
        assert claim["spec"]["storageClassName"] == "piraeus-ssd-2-autodiskful"
        assert claim["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
        mount = next(mount for mount in provider_pod["containers"][0]["volumeMounts"] if mount["name"] == "data")
        assert mount["subPath"] == "state"
        provider_config = remote.CANARY_CONFIG if instance == "myquest-canary" else remote.CONFIG
        assert provider_config.data is not None
        assert json.loads(provider_config.data[f"{instance}-integration.json"])["integrations"][0]["id"] == instance

        host = urlsplit(connection["origin"]).hostname
        service = resource("Service", instance)["spec"]
        assert service["selector"] == provider["template"]["metadata"]["labels"]
        assert {p["name"]: p["port"] for p in service["ports"]} == {"https": 8443, "grpc": 8444}
        assert connection["control_target"] == f"{host}:8444"
        assert resource("Certificate", instance)["spec"]["dnsNames"] == [host]
        monitor = resource("PodMonitor", instance)["spec"]
        assert monitor["selector"]["matchLabels"] == service["selector"]
        assert [endpoint["port"] for endpoint in monitor["podMetricsEndpoints"]] == ["metrics"]
        if instance == "quest-scheduler":
            assert monitor["selector"]["matchLabels"] == {"app": "platform-sessions-quest-scheduler"}
        assert "metrics" in {p["name"] for p in provider_pod["containers"][0]["ports"]}


def test_scheduler_has_one_owner_of_state(scratch_app: App) -> None:
    objects = Cdk8sTesting.synth(remote.chart(scratch_app, record_upstream_traffic=False))
    deployments = [item for item in objects if item["kind"] == "Deployment"]
    owners = [
        item
        for item in deployments
        if any(
            volume.get("persistentVolumeClaim", {}).get("claimName") == "platform-sessions-quest-scheduler-piraeus"
            for volume in item["spec"]["template"]["spec"]["volumes"]
        )
    ]
    assert [item["metadata"]["name"] for item in owners] == ["quest-scheduler"]
    assert remote.CONFIG.data is not None
    integration = json.loads(remote.CONFIG.data["quest-scheduler-integration.json"])["integrations"][0]
    assert integration["state_dir"] == "/data/session/quest-scheduler"


def test_myquest_type_gets_bounded_startup_grace_without_changing_liveness(scratch_app: App) -> None:
    for instance, provider_type in (("myquest-canary", "myquest"), ("other-canary", "openai")):
        chart = Chart(scratch_app, instance)
        remote._provider_workload(chart, instance, provider_type=provider_type, record_upstream_traffic=False)
        deployment = next(item for item in Cdk8sTesting.synth(chart) if item["kind"] == "Deployment")
        provider = deployment["spec"]["template"]["spec"]["containers"][0]
        health = {"httpGet": {"path": "/healthz", "port": "https", "scheme": "HTTPS"}}
        assert provider["readinessProbe"] == provider["livenessProbe"] == health
        if provider_type == "myquest":
            startup = provider["startupProbe"]
            assert startup["httpGet"] == health["httpGet"]
            assert 32 <= startup["periodSeconds"] * startup["failureThreshold"] <= 60
        else:
            assert "startupProbe" not in provider


def test_recording_wires_each_provider_to_its_private_hdd_capture(scratch_app: App) -> None:
    objects = Cdk8sTesting.synth(remote.chart(scratch_app, record_upstream_traffic=True))

    def resource(kind, name):
        return next(item for item in objects if item["kind"] == kind and item["metadata"]["name"] == name)

    assert config.RECORD_UPSTREAM_TRAFFIC
    for instance in (item for item in remote.PROVIDERS if item not in {"myquest", "myquest-canary"}):
        name = remote._provider_name(instance)
        pod = resource("Deployment", instance)["spec"]["template"]["spec"]
        containers = {container["name"]: container for container in pod["containers"]}
        assert set(containers) == {"provider"}
        expected_memory = (
            {"requests": "1Gi", "limits": "2Gi"} if instance == "myquest" else {"requests": "128Mi", "limits": "1Gi"}
        )
        assert {
            key: containers["provider"]["resources"][key]["memory"] for key in ("requests", "limits")
        } == expected_memory
        init = pod["initContainers"]
        assert [container["name"] for container in init] == ["recorder-ca-init", "upstream-recorder"]
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
        name = remote._provider_name(instance)
        pod = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == instance)[
            "spec"
        ]["template"]["spec"]
        assert [item["name"] for item in pod["containers"]] == ["provider"]
        assert not any(item["name"] == "HTTPS_PROXY" for item in pod["containers"][0]["env"])
        assert any(
            item["kind"] == "PersistentVolumeClaim" and item["metadata"]["name"] == f"{name}-captures"
            for item in objects
        )


def test_myquest_canary_is_direct_and_owner_only(scratch_app: App) -> None:
    objects = Cdk8sTesting.synth(remote.chart(scratch_app))
    canary = next(
        item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == "myquest-canary"
    )
    pod = canary["spec"]["template"]["spec"]
    provider = pod["containers"][0]
    production = next(
        item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == "myquest"
    )
    env = {item["name"]: item["value"] for item in provider["env"]}
    assert provider["image"] == f"{remote.REGISTRY}/platform-sessions-myquest:unset"
    assert provider["image"] == production["spec"]["template"]["spec"]["containers"][0]["image"]
    assert env["MYQUEST_BROWSER_RECORD_HAR"] == "true"
    assert env["PLATFORM_SESSIONS_PROXY_ENABLED"] == "false"
    assert env["MYQUEST_BROWSER_LAUNCH_MODE"] == "cdp"
    assert "HTTPS_PROXY" not in env
    assert "SSL_CERT_FILE" not in env
    assert not any(item["name"] == "upstream-recorder" for item in pod.get("initContainers", []))
    assert {
        volume["persistentVolumeClaim"]["claimName"] for volume in pod["volumes"] if "persistentVolumeClaim" in volume
    } == {"platform-sessions-myquest-canary-piraeus"}
    assert all(
        route["instance"] != "myquest-canary" for bearer in config.BEARERS.values() for route in bearer.get("allow", [])
    )
    assert remote.CANARY_CONFIG.data is not None
    assert json.loads(remote.CANARY_CONFIG.data["myquest-canary-integration.json"])["integrations"][0] == {
        "id": "myquest-canary",
        "type": "myquest",
        "state_dir": config.myquest_canary_state_dir(False),
        "poll_interval_seconds": 300,
    }


def test_myquest_canary_proxy_switch_is_isolated(scratch_app: App, monkeypatch) -> None:
    monkeypatch.setattr(config, "MYQUEST_CANARY_RECORD_UPSTREAM_TRAFFIC", True)
    proxy_config = remote.canary_config(config.MYQUEST_CANARY_RECORD_UPSTREAM_TRAFFIC)
    assert proxy_config.data is not None
    assert json.loads(proxy_config.data["myquest-canary-integration.json"])["integrations"] == [
        {
            "id": "myquest-canary",
            "type": "myquest",
            "state_dir": "/data/session/myquest-canary-proxy",
            "poll_interval_seconds": 300,
        }
    ]
    assert proxy_config.name == remote.CANARY_CONFIG.name
    objects = Cdk8sTesting.synth(remote.chart(scratch_app))
    deployments = {item["metadata"]["name"]: item for item in objects if item["kind"] == "Deployment"}
    canary_pod = deployments["myquest-canary"]["spec"]["template"]["spec"]
    env = {item["name"]: item["value"] for item in canary_pod["containers"][0]["env"]}
    assert env["PLATFORM_SESSIONS_PROXY_ENABLED"] == "true"
    assert env["MYQUEST_BROWSER_RECORD_HAR"] == "true"
    assert "MYQUEST_BROWSER_LAUNCH_MODE" not in env
    assert any(item["name"] == "upstream-recorder" for item in canary_pod["initContainers"])
    assert any(
        volume.get("persistentVolumeClaim", {}).get("claimName") == "platform-sessions-myquest-canary-captures"
        for volume in canary_pod["volumes"]
    )
    production_env = {
        item["name"]: item["value"]
        for item in deployments["myquest"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "MYQUEST_BROWSER_RECORD_HAR" not in production_env
    assert production_env["MYQUEST_BROWSER_LAUNCH_MODE"] == "cdp"
    assert production_env["PLATFORM_SESSIONS_PROXY_ENABLED"] == "false"
    assert "HTTPS_PROXY" not in production_env
    assert not deployments["myquest"]["spec"]["template"]["spec"].get("initContainers")
    assert (
        canary_pod["containers"][0]["image"]
        == deployments["myquest"]["spec"]["template"]["spec"]["containers"][0]["image"]
    )


if __name__ == "__main__":
    pytest_bazel.main()
