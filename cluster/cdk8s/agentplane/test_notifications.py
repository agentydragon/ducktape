"""Only the staging GitHub webhook is public; workload APIs stay behind egress auth."""

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s.agentplane import (
    actions as action_service,
    notifications,
    runner_gateway,
    session_dispatcher,
    staging,
)


def test_notifications_migrate_before_serving_and_roll(staging_manifests: list[dict[str, Any]]) -> None:
    notifications_deployment = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == notifications.NAME
    )
    pod = notifications_deployment["spec"]["template"]["spec"]
    server = one(item for item in pod["containers"] if item["name"] == "notifications")
    assert notifications_deployment["spec"]["strategy"]["type"] == "RollingUpdate"
    assert not pod.get("initContainers")
    assert server["startupProbe"]["grpc"] == {"port": server["readinessProbe"]["grpc"]["port"], "service": "livez"}
    assert server["startupProbe"]["periodSeconds"] == 5
    assert server["startupProbe"]["failureThreshold"] == 60

    history_deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "history-service"
    )
    assert history_deployment["spec"]["strategy"]["type"] == "RollingUpdate"


def test_public_notification_routes_are_webhook_only(staging_manifests: list[dict[str, Any]]) -> None:
    rules = [
        rule
        for doc in staging_manifests
        if doc["kind"] == "HTTPRoute"
        for rule in doc["spec"]["rules"]
        if any(backend["name"] == notifications.NAME for backend in rule.get("backendRefs", []))
    ]
    rule = one(rules)
    assert rule["matches"] == [{"path": {"type": "Exact", "value": "/v1/webhooks/github"}}]
    assert all(filter_["type"] == "ResponseHeaderModifier" for filter_ in rule.get("filters", []))


def test_only_staging_notifications_admit_gateway(staging_manifests: list[dict[str, Any]]) -> None:
    endpoint = notifications.http_service(staging.ENV.namespace)
    ingress = [
        rule
        for doc in staging_manifests
        if doc["kind"] == "CiliumNetworkPolicy"
        and doc["spec"]["endpointSelector"].get("matchLabels") == endpoint.pods.selector
        for rule in doc["spec"].get("ingress", [])
        if "ingress" in rule.get("fromEntities", [])
    ]
    assert one(ingress) == {
        "fromEntities": ["ingress"],
        "toPorts": [{"ports": [{"port": str(endpoint.pod_port), "protocol": "TCP"}]}],
    }


def test_github_secrets_are_server_only_and_not_in_yaml(staging_manifests: list[dict[str, Any]]) -> None:
    config = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == f"{notifications.NAME}-settings"
    )
    github = yaml.safe_load(config["data"]["settings.yaml"]).get("github")
    pod = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == notifications.NAME
    )["spec"]["template"]["spec"]
    prefix = "AGENTPLANE_NOTIFICATIONS_GITHUB__"
    assert not pod.get("initContainers")
    server = one(item for item in pod["containers"] if item["name"] == "notifications")
    variables = {item["name"].removeprefix(prefix): item for item in server["env"] if item["name"].startswith(prefix)}
    assert set(github) == {"app_id"}
    assert set(variables) == {"PRIVATE_KEY", "WEBHOOK_SECRET"}
    for field, key in [("PRIVATE_KEY", "private-key"), ("WEBHOOK_SECRET", "webhook-secret")]:
        assert "value" not in variables[field]
        reference = variables[field]["valueFrom"]["secretKeyRef"]
        assert reference["key"] == key
        assert not reference.get("optional", False)
    assert (
        variables["PRIVATE_KEY"]["valueFrom"]["secretKeyRef"]["name"]
        == variables["WEBHOOK_SECRET"]["valueFrom"]["secretKeyRef"]["name"]
    )


def test_notifications_use_the_configured_dispatcher(staging_manifests: list[dict[str, Any]]) -> None:
    config = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == f"{notifications.NAME}-settings"
    )
    settings = yaml.safe_load(config["data"]["settings.yaml"])
    actions = action_service.service(staging.ENV.namespace)
    assert settings["actions"] == {
        "url": f"http://{actions.fqdn}:{actions.port.number}/",
        "token_file": "/var/run/secrets/notifications/actions",
    }
    dispatcher = session_dispatcher.service(staging.ENV.namespace)
    assert settings["session_dispatcher"] == {
        "target": f"{dispatcher.fqdn}:{dispatcher.port.number}",
        "token_file": "/var/run/secrets/notifications/dispatcher",
    }

    deployment = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == notifications.NAME
    )
    pod = deployment["spec"]["template"]["spec"]
    token_volume = one(volume for volume in pod["volumes"] if volume["name"] == "service-tokens")
    tokens = {
        source["serviceAccountToken"]["path"]: source["serviceAccountToken"]
        for source in token_volume["projected"]["sources"]
    }
    assert tokens["actions"]["audience"] == WORKLOAD_TOKEN_AUDIENCE
    assert tokens["dispatcher"]["audience"] == session_dispatcher.TOKEN_AUDIENCE

    policy = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "CiliumNetworkPolicy"
        and doc["metadata"]["name"] == notifications.NAME.removeprefix("agentplane-")
    )["spec"]
    egress_services = {
        endpoint["matchLabels"].get("app.kubernetes.io/name")
        for rule in policy["egress"]
        for endpoint in rule.get("toEndpoints", [])
    }
    assert runner_gateway.NAME in egress_services


if __name__ == "__main__":
    pytest_bazel.main()
