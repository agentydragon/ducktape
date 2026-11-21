"""The durable sender front door and workload grants survive the Dispatcher and Gateway sharing one workload."""

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import actions, app, notifications, runner_gateway, staging
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource


def test_dispatcher_sender_identity_and_service_routing(staging_manifests: list[dict[str, Any]]) -> None:
    pod = find_resource(staging_manifests, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    container = one(pod["containers"])
    settings = config_map_yaml(staging_manifests, f"{runner_gateway.NAME}-settings", "settings.yaml")
    callers = set(settings["dispatcher_service_callers"])
    wakers = set(settings.get("wake_service_callers", []))
    senders = (app.NAME, notifications.NAME, actions.NAME)
    assert callers == {
        uri
        for sender in senders
        for uri in find_resource(staging_manifests, "Certificate", f"{sender}-workload")["spec"]["uris"]
    }
    assert wakers <= callers
    service = find_resource(staging_manifests, "Service", runner_gateway.NAME)["spec"]
    labels = find_resource(staging_manifests, "Deployment", runner_gateway.NAME)["spec"]["template"]["metadata"][
        "labels"
    ]
    assert service["selector"].items() <= labels.items()
    assert {port["targetPort"] for port in service["ports"]} <= {port["containerPort"] for port in container["ports"]}
    database = find_resource(staging_manifests, "Database", "session-dispatcher")["spec"]
    assert database["name"] == "session_dispatcher"
    assert database["owner"] == "session_dispatcher"
    assert {item["name"]: item.get("valueFrom") for item in container["env"]}[
        "AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL"
    ] == {"secretKeyRef": {"name": "postgres-session-dispatcher", "key": "uri"}}
    for sender in senders:
        policy = find_resource(staging_manifests, "CiliumNetworkPolicy", sender)["spec"]
        assert any(
            target["matchLabels"].get("app.kubernetes.io/name") == runner_gateway.NAME
            for rule in policy["egress"]
            for target in rule.get("toEndpoints", [])
        )


def test_app_reads_its_serving_database_uri(staging_manifests: list[dict[str, Any]]) -> None:
    pod = find_resource(staging_manifests, "Deployment", app.NAME)["spec"]["template"]["spec"]
    container = one(container for container in pod["containers"] if container["name"] == "app")
    database_url = one(variable for variable in container["env"] if variable["name"] == "AGENTPLANE_DATABASE_URL")
    assert database_url == {
        "name": "AGENTPLANE_DATABASE_URL",
        "valueFrom": {"secretKeyRef": {"name": "postgres-app", "key": "uri"}},
    }


def test_app_uses_dispatcher_grpc_target(staging_manifests: list[dict[str, Any]]) -> None:
    service = find_resource(staging_manifests, "Service", runner_gateway.sessions_service(staging.ENV.namespace).name)
    grpc_port = one(port for port in service["spec"]["ports"] if port["name"] == "grpc")
    endpoint = f"{service['metadata']['name']}.{staging.ENV.namespace}.svc.cluster.local:{grpc_port['port']}"
    app_config_map = find_resource(staging_manifests, "ConfigMap", "app-config")
    app_config = yaml.safe_load(app_config_map["data"]["config.yaml"])
    assert app_config["session_dispatcher_target"] == endpoint


if __name__ == "__main__":
    pytest_bazel.main()
