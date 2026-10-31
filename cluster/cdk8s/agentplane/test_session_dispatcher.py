"""The durable sender front door and workload grants survive the Dispatcher and Gateway sharing one workload."""

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from agentplane.runner_gateway.settings import Settings
from cluster.cdk8s.agentplane import actions, app, notifications, runner_gateway, session_dispatcher, staging
from util.settings_contract import env_name


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
    assert {item["name"]: item.get("value") for item in container["env"]}[
        env_name(Settings, "dispatcher_audience")
    ] == session_dispatcher.TOKEN_AUDIENCE
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
    database_url_name = env_name(app.Settings, "database_url")

    variables = container["env"]
    database_url = one(variable for variable in variables if variable["name"] == database_url_name)
    assert database_url == {
        "name": database_url_name,
        "valueFrom": {"secretKeyRef": {"name": "postgres-app", "key": "uri"}},
    }
    assert not any(variable["name"].startswith("AGENTPLANE_DB_") for variable in variables)
    assert container["startupProbe"]["httpGet"]["path"] == "/healthz"


def test_app_uses_dispatcher_grpc_target_without_legacy_http_flag(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == app.NAME
    )
    pod = deployment["spec"]["template"]["spec"]
    app_container = one(container for container in pod["containers"] if container["name"] == "app")
    assert (
        f"--session-dispatcher-target={session_dispatcher.service(staging.ENV.namespace).fqdn}:"
        f"{session_dispatcher.service(staging.ENV.namespace).port.number}"
    ) in app_container["args"]
    assert not any(arg.startswith("--session-dispatcher-http-url=") for arg in app_container["args"])


if __name__ == "__main__":
    pytest_bazel.main()
