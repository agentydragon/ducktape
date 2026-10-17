"""The durable sender front door and workload grants survive the Dispatcher and Gateway sharing one workload."""

from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane import app, notifications, runner_gateway, session_dispatcher
from cluster.cdk8s.agentplane.conftest import NAMESPACES
from util.settings_contract import env_name


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_dispatcher_sender_identity_and_service_routing(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    documents = agentplane_manifests[namespace]

    def resource(kind: str, name: str) -> dict[str, Any]:
        return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)

    pod = resource("Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    container = one(pod["containers"])
    callers = {
        arg.removeprefix("--dispatcher-caller=") for arg in container["args"] if arg.startswith("--dispatcher-caller=")
    }
    wakers = {arg.removeprefix("--wake-caller=") for arg in container["args"] if arg.startswith("--wake-caller=")}
    assert callers == {f"{namespace}/{app.NAME}", f"{namespace}/{notifications.NAME}"}
    assert wakers <= callers
    assert f"--dispatcher-audience={session_dispatcher.TOKEN_AUDIENCE}" in container["args"]
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
    for sender in (app.NAME, notifications.NAME):
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


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_app_and_migration_use_same_database_uri_secret(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    deployment = one(
        doc
        for doc in agentplane_manifests[namespace]
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == app.NAME
    )
    pod = deployment["spec"]["template"]["spec"]
    containers = [
        one(container for container in pod["containers"] if container["name"] == "app"),
        one(container for container in pod["initContainers"] if container["name"] == "migrate"),
    ]
    database_url_name = env_name(app.Settings, "database_url")

    for container in containers:
        variables = container["env"]
        database_url = one(variable for variable in variables if variable["name"] == database_url_name)
        assert database_url == {
            "name": database_url_name,
            "valueFrom": {"secretKeyRef": {"name": "postgres-app", "key": "uri"}},
        }
        assert not any(variable["name"].startswith("AGENTPLANE_DB_") for variable in variables)


if __name__ == "__main__":
    pytest_bazel.main()
