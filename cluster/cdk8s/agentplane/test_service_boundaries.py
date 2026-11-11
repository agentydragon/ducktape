"""Staging manifests keep each service to its own data and authority: History owns its database, the Runner
Gateway alone mutates Sandboxes, and Notifications never calls the app."""

from typing import Any

import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane import app, history_service, notifications, runner_gateway, session_dispatcher


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def test_history_service_uses_sandbox_database(staging_manifests: list[dict[str, Any]]) -> None:
    history = _resource(staging_manifests, "Deployment", history_service.NAME)["spec"]["template"]["spec"]
    history_database = {"name": "postgres-sandbox-service", "key": "uri"}
    history_external_secret = _resource(staging_manifests, "ExternalSecret", "postgres-sandbox-service")
    assert history_external_secret["spec"]["target"]["name"] == "postgres-sandbox-service"
    history_database_resource = _resource(staging_manifests, "Database", "sandbox-service")
    assert history_database_resource["spec"]["name"] == "sandbox_service"
    assert history_database_resource["spec"]["owner"] == "sandbox_service"
    assert any(
        value.get("secretKeyRef") == history_database
        for container in history["containers"]
        for value in (item.get("valueFrom", {}) for item in container["env"])
    )


def test_gateway_owns_sandbox_mutations(staging_manifests: list[dict[str, Any]]) -> None:
    app_rules = _resource(staging_manifests, "Role", app.NAME.removeprefix("agentplane-"))["rules"]
    for rule in app_rules:
        if rule["resources"] == ["sandboxes"]:
            assert set(rule["verbs"]) == {"get", "list", "watch"}
    permissions = {(tuple(rule["apiGroups"]), tuple(rule["resources"]), tuple(rule["verbs"])) for rule in app_rules}
    assert (("agentplane.allegedly.works",), ("sandboxtemplates",), ("list",)) in permissions
    assert (("subresources.kubevirt.io",), ("virtualmachineinstances/console",), ("get",)) in permissions

    gateway = _resource(staging_manifests, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    assert gateway["serviceAccountName"] == runner_gateway.NAME
    gateway_rules = _resource(staging_manifests, "Role", runner_gateway.NAME.removeprefix("agentplane-"))["rules"]
    assert any(
        rule["resources"] == ["sandboxes"] and {"create", "patch", "delete"} <= set(rule["verbs"])
        for rule in gateway_rules
    )


def test_notifications_use_current_service_tokens_without_app_egress(staging_manifests: list[dict[str, Any]]) -> None:
    pod = _resource(staging_manifests, "Deployment", notifications.NAME)["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == notifications.NAME
    tokens = one(volume for volume in pod["volumes"] if volume["name"] == "service-tokens")
    assert {source["serviceAccountToken"]["audience"] for source in tokens["projected"]["sources"]} == {
        "agentplane-egress",
        runner_gateway.TOKEN_AUDIENCE,
        history_service.TOKEN_AUDIENCE,
        session_dispatcher.TOKEN_AUDIENCE,
    }
    policy = _resource(staging_manifests, "CiliumNetworkPolicy", notifications.NAME.removeprefix("agentplane-"))["spec"]
    assert not any(
        target["matchLabels"].get("app.kubernetes.io/name") == app.NAME
        for rule in policy["egress"]
        for target in rule.get("toEndpoints", [])
    )


if __name__ == "__main__":
    pytest_bazel.main()
