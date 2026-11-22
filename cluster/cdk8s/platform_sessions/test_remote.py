"""The gateway and provider workloads agree on their connections and storage."""

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_bazel
from cdk8s import App

from cluster.cdk8s.platform_sessions import app, config, remote
from cluster.cdk8s.testing.manifests import find_resource


@pytest.fixture(scope="module")
def direct_workloads(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "MYQUEST_CANARY_RECORD_UPSTREAM_TRAFFIC", False)
        return remote.chart(scratch_app_factory(), record_upstream_traffic=False).to_json()


def test_remote_workloads_share_connections_but_not_legacy_storage(
    scratch_app: App, direct_workloads: list[dict[str, Any]]
) -> None:
    gateway = find_resource(direct_workloads, "Deployment", "gateway")["spec"]
    gateway_pod = gateway["template"]["spec"]
    gateway_container = gateway_pod["containers"][0]
    # The Go gateway uses its default listener port.
    listener_ports = {port["name"]: port["containerPort"] for port in gateway_container["ports"]}
    assert listener_ports == {"http": 8080}
    gateway_service = find_resource(direct_workloads, "Service", "gateway")["spec"]
    [gateway_port] = gateway_service["ports"]
    assert gateway_port["targetPort"] == "http"
    assert gateway_service["selector"] == gateway["template"]["metadata"]["labels"]
    assert all("persistentVolumeClaim" not in volume for volume in gateway_pod["volumes"])
    public_objects = app.chart(scratch_app).to_json()
    public_service = find_resource(public_objects, "Service", app.NAME)
    assert public_service["spec"]["selector"] == gateway_service["selector"]
    [public_port] = public_service["spec"]["ports"]
    assert public_port["port"] == gateway_port["port"]
    assert public_port["targetPort"] == listener_ports["http"]

    assert remote.CONFIG.data is not None
    assert remote.CANARY_CONFIG.data is not None
    connections = json.loads(remote.CANARY_CONFIG.data["connections.json"])
    gateway_integrations = json.loads(remote.CANARY_CONFIG.data["gateway-integrations.json"])["integrations"]
    assert set(connections) == {item["id"] for item in gateway_integrations}
    assert "myquest-canary" not in json.loads(remote.CONFIG.data["connections.json"])
    assert {
        find_resource(direct_workloads, "Deployment", instance)["spec"]["template"]["spec"]["containers"][0]["image"]
        for instance in ("openai", "anthropic", "thrive")
    } == {f"{remote.REGISTRY}/{remote.SHARED_PROVIDERS_IMAGE}:unset"}
    assert (
        sum(
            item["kind"] == "ImagePolicy" and item["metadata"]["name"] == remote.SHARED_PROVIDERS_IMAGE
            for item in direct_workloads
        )
        == 1
    )
    for instance, connection in connections.items():
        name = remote._provider_name(instance)
        provider = find_resource(direct_workloads, "Deployment", instance)["spec"]
        assert provider["strategy"]["type"] == "Recreate"
        provider_pod = provider["template"]["spec"]
        assert next(volume for volume in provider_pod["volumes"] if volume["name"] == "config")["configMap"][
            "name"
        ] == (remote.CANARY_CONFIG.name if instance == "myquest-canary" else remote.CONFIG.name)
        args = provider_pod["containers"][0]["args"]
        assert ("--route-grants" in args) == (instance not in {"blueshield", "myquest", "myquest-canary"})
        assert provider_pod["nodeSelector"]["topology.kubernetes.io/zone"] == "hil-ovh"
        claims = [
            v["persistentVolumeClaim"]["claimName"] for v in provider_pod["volumes"] if "persistentVolumeClaim" in v
        ]
        assert claims == [f"{name}-piraeus"]
        claim = find_resource(direct_workloads, "PersistentVolumeClaim", f"{name}-piraeus")
        assert claim["spec"]["storageClassName"] == "piraeus-ssd-2-autodiskful"
        assert claim["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
        mount = next(mount for mount in provider_pod["containers"][0]["volumeMounts"] if mount["name"] == "data")
        assert mount["subPath"] == "state"
        provider_config = remote.CANARY_CONFIG if instance == "myquest-canary" else remote.CONFIG
        assert provider_config.data is not None
        assert json.loads(provider_config.data[f"{instance}-integration.json"])["integrations"][0]["id"] == instance

        origin = urlsplit(connection["origin"])
        host = origin.hostname
        service = find_resource(direct_workloads, "Service", instance)["spec"]
        assert service["selector"] == provider["template"]["metadata"]["labels"]
        ports = {port["name"]: port for port in service["ports"]}
        assert set(ports) == {"https", "grpc"}
        assert origin.port == ports["https"]["port"]
        assert connection["control_target"] == f"{host}:{ports['grpc']['port']}"
        assert find_resource(direct_workloads, "Certificate", instance)["spec"]["dnsNames"] == [host]
        monitor = find_resource(direct_workloads, "PodMonitor", instance)["spec"]
        assert monitor["selector"]["matchLabels"] == service["selector"]
        [endpoint] = monitor["podMetricsEndpoints"]
        assert endpoint["port"] == "metrics"
        assert endpoint["port"] in {port["name"] for port in provider_pod["containers"][0]["ports"]}


def test_scheduler_has_one_owner_of_state(direct_workloads: list[dict[str, Any]]) -> None:
    deployments = [item for item in direct_workloads if item["kind"] == "Deployment"]
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


def test_myquest_type_gets_bounded_startup_grace_without_changing_liveness(
    direct_workloads: list[dict[str, Any]],
) -> None:
    health = {"httpGet": {"path": "/healthz", "port": "https", "scheme": "HTTPS"}}
    for integration in config.INTEGRATIONS["integrations"]:
        deployment = find_resource(direct_workloads, "Deployment", str(integration["id"]))
        containers = deployment["spec"]["template"]["spec"]["containers"]
        provider = next(container for container in containers if container["name"] == "provider")
        assert provider["readinessProbe"] == provider["livenessProbe"] == health
        if integration["type"] == "myquest":
            startup = provider["startupProbe"]
            assert startup["httpGet"] == health["httpGet"]
            assert 32 <= startup["periodSeconds"] * startup["failureThreshold"] <= 60
        else:
            assert "startupProbe" not in provider


def test_recording_wires_each_provider_to_its_private_hdd_capture(scratch_app: App) -> None:
    objects = remote.chart(scratch_app, record_upstream_traffic=True).to_json()

    for instance in (item for item in remote.PROVIDERS if item not in {"myquest", "myquest-canary"}):
        name = remote._provider_name(instance)
        pod = find_resource(objects, "Deployment", instance)["spec"]["template"]["spec"]
        containers = {container["name"]: container for container in pod["containers"]}
        assert set(containers) == {"provider"}
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
        claim = find_resource(objects, "PersistentVolumeClaim", f"{name}-captures")
        assert claim["spec"]["storageClassName"] == "piraeus-hdd-local"
        assert claim["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
        capture = next(volume for volume in pod["volumes"] if volume["name"] == "captures")
        assert capture["persistentVolumeClaim"]["claimName"] == f"{name}-captures"
        assert not any(mount["name"] == "captures" for mount in containers["provider"]["volumeMounts"])


def test_disabled_recording_keeps_capture_claims_without_proxy(direct_workloads: list[dict[str, Any]]) -> None:
    for instance in remote.PROVIDERS:
        name = remote._provider_name(instance)
        pod = find_resource(direct_workloads, "Deployment", instance)["spec"]["template"]["spec"]
        assert [item["name"] for item in pod["containers"]] == ["provider"]
        assert not any(item["name"] == "HTTPS_PROXY" for item in pod["containers"][0]["env"])
        assert any(
            item["kind"] == "PersistentVolumeClaim" and item["metadata"]["name"] == f"{name}-captures"
            for item in direct_workloads
        )


@pytest.mark.parametrize(
    ("record_upstream_traffic", "state_dir"),
    [
        pytest.param(False, "/data/session/myquest-canary-direct", id="direct"),
        pytest.param(True, "/data/session/myquest-canary-proxy", id="proxied"),
    ],
)
def test_myquest_recording_switch_is_canary_only(
    scratch_app: App, monkeypatch: pytest.MonkeyPatch, record_upstream_traffic: bool, state_dir: str
) -> None:
    monkeypatch.setattr(config, "MYQUEST_CANARY_RECORD_UPSTREAM_TRAFFIC", record_upstream_traffic)
    # CANARY_CONFIG is created at import. Use a distinct name so stale references fail.
    canary_config = remote.canary_config(record_upstream_traffic).model_copy(update={"name": "canary-mode-test"})
    monkeypatch.setattr(remote, "CANARY_CONFIG", canary_config)
    assert canary_config.data is not None
    [integration] = json.loads(canary_config.data["myquest-canary-integration.json"])["integrations"]
    assert integration["state_dir"] == state_dir
    assert integration["id"] == "myquest-canary"
    assert integration["type"] == "myquest"

    # Exercise both directions of the canary override.
    objects = remote.chart(scratch_app, record_upstream_traffic=not record_upstream_traffic).to_json()
    gateway = find_resource(objects, "Deployment", "gateway")["spec"]["template"]["spec"]
    [gateway_container] = gateway["containers"]
    refs = {
        env["name"]: env["valueFrom"]["configMapKeyRef"]
        for env in gateway_container["env"]
        if "configMapKeyRef" in env.get("valueFrom", {})
    }
    assert refs["PLATFORM_SESSIONS_INTEGRATION_CONFIG"] == {
        "name": canary_config.name,
        "key": "gateway-integrations.json",
    }
    assert refs["PLATFORM_SESSIONS_PROVIDER_CONNECTIONS"] == {"name": canary_config.name, "key": "connections.json"}
    canary = find_resource(objects, "Deployment", "myquest-canary")["spec"]["template"]["spec"]
    if record_upstream_traffic:
        volumes = {volume["name"]: volume for volume in canary["volumes"]}
        ca_secret = volumes["recorder-ca"]["secret"]["secretName"]
        assert any(obj["kind"] == "Certificate" and obj["spec"]["secretName"] == ca_secret for obj in objects)
        find_resource(objects, "Bundle", volumes["recorder-trust"]["configMap"]["name"])
    config_volume = next(volume for volume in canary["volumes"] if volume["name"] == "config")
    assert config_volume["configMap"]["name"] == canary_config.name
    production = find_resource(objects, "Deployment", "myquest")["spec"]["template"]["spec"]
    [canary_provider] = canary["containers"]
    [production_provider] = production["containers"]
    assert canary_provider["image"] == production_provider["image"]

    env = {item["name"]: item["value"] for item in canary_provider["env"]}
    assert env["MYQUEST_BROWSER_RECORD_HAR"] == "true"
    assert env["PLATFORM_SESSIONS_PROXY_ENABLED"] == str(record_upstream_traffic).lower()
    assert ("HTTPS_PROXY" in env) is record_upstream_traffic
    assert ("SSL_CERT_FILE" in env) is record_upstream_traffic
    assert env.get("MYQUEST_BROWSER_LAUNCH_MODE") == (None if record_upstream_traffic else "cdp")
    assert (
        any(item["name"] == "upstream-recorder" for item in canary.get("initContainers", [])) is record_upstream_traffic
    )
    canary_claims = {
        volume["persistentVolumeClaim"]["claimName"]
        for volume in canary["volumes"]
        if "persistentVolumeClaim" in volume
    }
    expected_claims = {"platform-sessions-myquest-canary-piraeus"}
    if record_upstream_traffic:
        expected_claims.add("platform-sessions-myquest-canary-captures")
    assert canary_claims == expected_claims

    production_env = {item["name"]: item["value"] for item in production_provider["env"]}
    assert "MYQUEST_BROWSER_RECORD_HAR" not in production_env
    assert production_env["MYQUEST_BROWSER_LAUNCH_MODE"] == "cdp"
    assert production_env["PLATFORM_SESSIONS_PROXY_ENABLED"] == "false"
    assert "HTTPS_PROXY" not in production_env
    assert not production.get("initContainers")


def test_myquest_canary_has_no_consumer_bearer_grant() -> None:
    assert remote.CONFIG.data is not None
    bearers = json.loads(remote.CONFIG.data["bearers.json"])
    assert all(
        route["instance"] != "myquest-canary" for bearer in bearers.values() for route in bearer.get("allow", [])
    )


if __name__ == "__main__":
    pytest_bazel.main()
