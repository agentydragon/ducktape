"""The bare-k3s fixture runs the Go Gateway while keeping generated Service names."""

from typing import Any

import pytest_bazel
import yaml

from agentplane.x.k3s_env import render
from util.bazel.runfiles import get_required_path


def _generated_manifest() -> list[dict[str, Any]]:
    path = get_required_path("_main/cluster/generated/agentplane-testing/agentplane-testing.k8s.yaml")
    return list(yaml.safe_load_all(path.read_text()))


def test_generated_fixture_uses_go_gateway_and_goose_queue_migration() -> None:
    docs = render.rewrite(_generated_manifest(), overrides={}, node_ip="127.0.0.1")
    deployments = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "Deployment"}
    assert render.SESSION_DISPATCHER not in deployments

    gateway = deployments[render.RUNNER_GATEWAY]
    pod = gateway["spec"]["template"]["spec"]
    container = pod["containers"][0]
    database_env = next(
        env for env in container["env"] if env["name"] == "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL"
    )
    database_secret = database_env["valueFrom"]["secretKeyRef"]
    # Kubernetes completes init containers before starting the serving container.
    migrator = next(init for init in pod["initContainers"] if init["name"] == "goose-migrate")
    migration_env = next(env for env in migrator["env"] if env["name"] == "DATABASE_URL")
    assert migration_env["valueFrom"]["secretKeyRef"] == database_secret

    args = set(container["args"])
    token_mount = next(mount for mount in container["volumeMounts"] if mount["name"] == "runner-gateway-token")
    token_volume = next(volume for volume in pod["volumes"] if volume["name"] == "runner-gateway-token")
    token = token_volume["projected"]["sources"][0]["serviceAccountToken"]
    assert f"--gateway-audience={token['audience']}" in args
    assert f"--runner-token-file={token_mount['mountPath']}/{token['path']}" in args

    services = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "Service"}
    gateway_service = services[render.RUNNER_GATEWAY]
    gateway_listener_ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    assert gateway_service["spec"]["selector"] == gateway["spec"]["selector"]["matchLabels"]
    assert next(port for port in gateway_service["spec"]["ports"] if port["name"] == "grpc")["port"] == 8080
    assert all(
        port["targetPort"] in gateway_listener_ports or port["targetPort"] in gateway_listener_ports.values()
        for port in gateway_service["spec"]["ports"]
    )
    dispatcher_service = services[render.SESSION_DISPATCHER]
    dispatcher_port = next(port for port in dispatcher_service["spec"]["ports"] if port["name"] == "grpc")
    dispatcher_listener = next(port for port in container["ports"] if port["name"] == "grpc")
    assert dispatcher_service["spec"]["clusterIP"] == "None"
    assert dispatcher_service["spec"]["selector"] == gateway["spec"]["selector"]["matchLabels"]
    assert dispatcher_port["port"] == dispatcher_port["targetPort"] == dispatcher_listener["containerPort"]

    endpoint = f"{render.SESSION_DISPATCHER}.{render.NAMESPACE}.svc.cluster.local:{dispatcher_port['port']}"
    app = deployments["app"]
    app_args = [arg for spec in app["spec"]["template"]["spec"]["containers"] for arg in spec["args"]]
    assert f"--session-dispatcher-target={endpoint}" in app_args
    notifications = next(
        doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "notifications-settings"
    )
    assert endpoint in notifications["data"]["settings.yaml"]

    gateway_role = next(
        doc for doc in docs if doc["kind"] == "Role" and doc["metadata"]["name"] == render.RUNNER_GATEWAY
    )
    sandbox_rule = next(rule for rule in gateway_role["rules"] if "sandboxes" in rule["resources"])
    assert "patch" in sandbox_rule["verbs"]
    assert f"--wake-caller={render.NAMESPACE}/app" in args


if __name__ == "__main__":
    pytest_bazel.main()
