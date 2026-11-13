"""The durable sender front door and workload grants survive the Dispatcher and Gateway sharing one workload."""

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import actions, app, notifications, runner_gateway, session_dispatcher, staging


def test_dispatcher_sender_identity_and_service_routing(staging_manifests: list[dict[str, Any]]) -> None:

    def resource(kind: str, name: str) -> dict[str, Any]:
        return one(doc for doc in staging_manifests if doc["kind"] == kind and doc["metadata"]["name"] == name)

    pod = resource("Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    container = one(pod["containers"])
    settings = yaml.safe_load(resource("ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"])
    callers = set(settings["dispatcher_callers"])
    wakers = set(settings.get("wake_callers", []))
    senders = (app.NAME, notifications.NAME, actions.NAME)
    assert callers == {f"{staging.ENV.namespace}/{sender}" for sender in senders}
    assert wakers <= callers
    assert settings["dispatcher_audience"] == session_dispatcher.TOKEN_AUDIENCE
    service = resource("Service", session_dispatcher.NAME)["spec"]
    labels = resource("Deployment", runner_gateway.NAME)["spec"]["template"]["metadata"]["labels"]
    assert service["selector"].items() <= labels.items()
    assert {port["targetPort"] for port in service["ports"]} <= {port["containerPort"] for port in container["ports"]}
    database = resource("Database", "session-dispatcher")["spec"]
    assert database["name"] == "session_dispatcher"
    assert database["owner"] == "session_dispatcher"
    assert {item["name"]: item.get("valueFrom") for item in container["env"]}[
        "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL"
    ] == {"secretKeyRef": {"name": "postgres-session-dispatcher", "key": "uri"}}
    for sender in senders:
        sender_pod = resource("Deployment", sender)["spec"]["template"]["spec"]
        assert any(
            source.get("serviceAccountToken", {}).get("audience") == session_dispatcher.TOKEN_AUDIENCE
            for volume in sender_pod["volumes"]
            for source in volume.get("projected", {}).get("sources", [])
        )
        policy = resource("CiliumNetworkPolicy", sender)["spec"]
        assert any(
            target["matchLabels"].get("app.kubernetes.io/name") == runner_gateway.NAME
            for rule in policy["egress"]
            for target in rule.get("toEndpoints", [])
        )


def test_app_migrates_with_its_serving_database_uri(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == app.NAME
    )
    pod = deployment["spec"]["template"]["spec"]

    assert not pod.get("initContainers")
    container = one(container for container in pod["containers"] if container["name"] == "app")

    variables = container["env"]
    database_url = one(variable for variable in variables if variable["name"] == "AGENTPLANE_DATABASE_URL")
    assert database_url == {
        "name": "AGENTPLANE_DATABASE_URL",
        "valueFrom": {"secretKeyRef": {"name": "postgres-app", "key": "uri"}},
    }
    assert not any(variable["name"].startswith("AGENTPLANE_DB_") for variable in variables)
    assert container["startupProbe"]["httpGet"]["path"] == "/healthz"


def test_app_uses_dispatcher_grpc_target(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == app.NAME
    )
    pod = deployment["spec"]["template"]["spec"]
    app_container = one(container for container in pod["containers"] if container["name"] == "app")
    service = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "Service" and doc["metadata"]["name"] == session_dispatcher.NAME
    )
    grpc_port = one(port for port in service["spec"]["ports"] if port["name"] == "grpc")
    endpoint = f"{service['metadata']['name']}.{staging.ENV.namespace}.svc.cluster.local:{grpc_port['port']}"
    app_config_map = one(
        doc for doc in staging_manifests if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "app-config"
    )
    app_config = yaml.safe_load(app_config_map["data"]["config.yaml"])
    assert app_config["session_dispatcher_target"] == endpoint
    assert app_container.get("args", []) == []


if __name__ == "__main__":
    pytest_bazel.main()
