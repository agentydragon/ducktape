"""The service HTTP listener is webhook-only; workload APIs use its gRPC listener."""

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from agentplane.environment_runtime_transport.config import ACTIONS_URI, NOTIFICATIONS_URI, SESSION_DISPATCHER_URI
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s.agentplane import (
    actions as action_service,
    notifications,
    runner_gateway,
    session_dispatcher,
    staging,
    workload_tls,
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
    startup = server["startupProbe"]["exec"]["command"]
    readiness = server["readinessProbe"]["exec"]["command"]
    assert startup[:2] == readiness[:2] == ["/healthprobe", "--target"]
    assert "127.0.0.1:8080" in startup
    assert NOTIFICATIONS_URI in startup
    assert "--service" in startup
    assert "livez" in startup

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
    endpoint = notifications.webhook_service(staging.ENV.namespace)
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
    assert settings["actions"]["url"] == f"https://{actions.fqdn}:{actions.port.number}/"
    assert settings["actions"]["token_file"] == "/var/run/secrets/notifications/actions"
    dispatcher = session_dispatcher.service(staging.ENV.namespace)
    assert settings["session_dispatcher"]["target"] == f"{dispatcher.fqdn}:{dispatcher.port.number}"
    assert settings["session_dispatcher"]["token_file"] == "/var/run/secrets/notifications/dispatcher"
    assert settings["actions"]["tls"] == {"ca_file": workload_tls.TRUST_BUNDLE_FILE, "server_uri": ACTIONS_URI}
    assert settings["session_dispatcher"]["tls"] == {
        "ca_file": workload_tls.TRUST_BUNDLE_FILE,
        "server_uri": SESSION_DISPATCHER_URI,
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
    proxy_ingress = one(
        rule
        for rule in policy["ingress"]
        if any(
            endpoint["matchLabels"].get("app.kubernetes.io/name") == "egress"
            for endpoint in rule.get("fromEndpoints", [])
        )
    )
    assert proxy_ingress == {
        "fromEndpoints": [
            {
                "matchLabels": {
                    "k8s:io.kubernetes.pod.namespace": staging.ENV.namespace,
                    "app.kubernetes.io/name": "egress",
                }
            }
        ],
        "toPorts": [
            {"ports": [{"port": str(notifications.service(staging.ENV.namespace).pod_port), "protocol": "TCP"}]}
        ],
    }


if __name__ == "__main__":
    pytest_bazel.main()
