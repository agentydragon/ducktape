"""Staging manifests keep each service to its own data and authority: History owns its database, the Sandbox
Controller owns environment management, and Notifications never calls the app."""

from typing import Any

import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane import app, history_service, notifications, runner_gateway, sandbox_controller
from cluster.cdk8s.testing.manifests import find_resource


def test_history_service_uses_sandbox_database(staging_manifests: list[dict[str, Any]]) -> None:
    history = find_resource(staging_manifests, "Deployment", history_service.NAME)["spec"]["template"]["spec"]
    history_database = {"name": "postgres-sandbox-service", "key": "uri"}
    history_external_secret = find_resource(staging_manifests, "ExternalSecret", "postgres-sandbox-service")
    assert history_external_secret["spec"]["target"]["name"] == "postgres-sandbox-service"
    history_database_resource = find_resource(staging_manifests, "Database", "sandbox-service")
    assert history_database_resource["spec"]["name"] == "sandbox_service"
    assert history_database_resource["spec"]["owner"] == "sandbox_service"
    assert any(
        value.get("secretKeyRef") == history_database
        for container in history["containers"]
        for value in (item.get("valueFrom", {}) for item in container["env"])
    )


def test_environment_management_and_dispatcher_wake_have_separate_authority(
    staging_manifests: list[dict[str, Any]],
) -> None:
    app_rules = find_resource(staging_manifests, "Role", app.NAME)["rules"]
    for rule in app_rules:
        if rule["resources"] == ["sandboxes"]:
            assert set(rule["verbs"]) == {"get", "list", "watch"}
    permissions = {(tuple(rule["apiGroups"]), tuple(rule["resources"]), tuple(rule["verbs"])) for rule in app_rules}
    assert (("agentplane.allegedly.works",), ("sandboxtemplates",), ("list",)) in permissions
    assert (("subresources.kubevirt.io",), ("virtualmachineinstances/console",), ("get",)) in permissions

    gateway = find_resource(staging_manifests, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    assert gateway["serviceAccountName"] == runner_gateway.NAME
    gateway_rules = find_resource(staging_manifests, "Role", runner_gateway.NAME)["rules"]
    assert set(one(rule for rule in gateway_rules if rule["resources"] == ["sandboxes"])["verbs"]) == {
        "get",
        "list",
        "watch",
        "patch",
    }
    assert not any("sandboxtemplates" in rule["resources"] for rule in gateway_rules)

    controller = find_resource(staging_manifests, "Deployment", sandbox_controller.NAME)["spec"]["template"]["spec"]
    assert controller["serviceAccountName"] == sandbox_controller.NAME
    controller_rules = find_resource(staging_manifests, "Role", sandbox_controller.NAME)["rules"]
    assert {"create", "patch", "delete"} <= set(
        one(rule for rule in controller_rules if rule["resources"] == ["sandboxes"])["verbs"]
    )
    assert {"get", "list"} <= set(
        one(rule for rule in controller_rules if rule["resources"] == ["sandboxtemplates"])["verbs"]
    )


def test_notifications_have_their_own_account_without_app_egress(staging_manifests: list[dict[str, Any]]) -> None:
    pod = find_resource(staging_manifests, "Deployment", notifications.NAME)["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == notifications.NAME
    policy = find_resource(staging_manifests, "CiliumNetworkPolicy", notifications.NAME)["spec"]
    assert not any(
        target["matchLabels"].get("app.kubernetes.io/name") == app.NAME
        for rule in policy["egress"]
        for target in rule.get("toEndpoints", [])
    )


if __name__ == "__main__":
    pytest_bazel.main()
