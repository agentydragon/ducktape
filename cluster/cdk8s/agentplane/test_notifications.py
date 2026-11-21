"""The service HTTP listener is webhook-only; workload APIs use its gRPC listener."""

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from agentplane.net.peertls.config import APP_URI
from cluster.cdk8s.agentplane import actions as action_service, notifications, runner_gateway, staging
from cluster.cdk8s.testing.manifests import find_resource


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
    config = find_resource(staging_manifests, "ConfigMap", f"{notifications.NAME}-settings")
    settings = yaml.safe_load(config["data"]["settings.yaml"])
    assert settings["operator_reader_services"] == [APP_URI]
    assert [auth["mode"] for auth in settings["rpc"]["auth"]] == ["spiffe"]
    github = settings.get("github")
    pod = find_resource(staging_manifests, "Deployment", notifications.NAME)["spec"]["template"]["spec"]
    prefix = "AGENTPLANE_NOTIFICATIONS_GITHUB__"
    server = one(item for item in pod["containers"] if item["name"] == "notifications")
    variables = {item["name"].removeprefix(prefix): item for item in server["env"] if item["name"].startswith(prefix)}
    assert set(github) == {
        "app_id",
        "api_url",
        "request_timeout_s",
        "api_version",
        "freshness_seconds",
        "max_body_bytes",
        "webhook_concurrency",
    }
    assert github["api_version"] == "2022-11-28"
    assert isinstance(github["api_version"], str)
    assert "database_url" not in settings
    assert "log_levels" not in settings
    assert set(variables) == {"PRIVATE_KEY", "WEBHOOK_SECRET"}
    assert {item["name"] for item in server["env"] if item["name"].startswith("AGENTPLANE_NOTIFICATIONS_")} == {
        "AGENTPLANE_NOTIFICATIONS_CONFIG_FILE",
        "AGENTPLANE_NOTIFICATIONS_DATABASE_URL",
        "AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY",
        "AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET",
    }
    for field, key in [("PRIVATE_KEY", "private-key"), ("WEBHOOK_SECRET", "webhook-secret")]:
        assert "value" not in variables[field]
        reference = variables[field]["valueFrom"]["secretKeyRef"]
        assert reference["key"] == key
        assert not reference.get("optional", False)
    assert (
        variables["PRIVATE_KEY"]["valueFrom"]["secretKeyRef"]["name"]
        == variables["WEBHOOK_SECRET"]["valueFrom"]["secretKeyRef"]["name"]
    )


def test_notifications_use_the_configured_backend_targets(staging_manifests: list[dict[str, Any]]) -> None:
    config = find_resource(staging_manifests, "ConfigMap", f"{notifications.NAME}-settings")
    settings = yaml.safe_load(config["data"]["settings.yaml"])
    actions = action_service.service(staging.ENV.namespace)
    assert settings["actions"]["url"] == f"https://{actions.fqdn}:{actions.port.number}/"
    dispatcher = runner_gateway.sessions_service(staging.ENV.namespace)
    assert settings["session_dispatcher"]["target"] == f"{dispatcher.fqdn}:{dispatcher.port.number}"
    policy = find_resource(staging_manifests, "CiliumNetworkPolicy", notifications.NAME)["spec"]
    egress_services = {
        endpoint["matchLabels"].get("app.kubernetes.io/name")
        for rule in policy["egress"]
        for endpoint in rule.get("toEndpoints", [])
    }
    assert dispatcher.name in egress_services
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
