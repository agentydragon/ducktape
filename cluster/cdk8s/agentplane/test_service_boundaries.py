"""Staging manifests keep each service to its own data and authority: History owns its database, the Sandbox
Controller owns environment management, and Notifications never calls the app."""

from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_bazel
from more_itertools import one

from agentplane.crd_group import GROUP
from cluster.cdk8s.agent_rbac_test_support import Permission, Rbac, uncovered
from cluster.cdk8s.agentplane import app, history_service, notifications, runner_gateway, sandbox_controller
from cluster.cdk8s.testing.manifests import find_resource


def test_history_service_uses_sandbox_database(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = find_resource(staging_manifests, "Deployment", history_service.NAME)
    namespace = deployment["metadata"]["namespace"]
    pod = deployment["spec"]["template"]["spec"]
    service = one(container for container in pod["containers"] if container["name"] == history_service.NAME)
    url = one(var for var in service["env"] if var["name"] == "AGENTPLANE_HISTORY_SERVICE_DATABASE_URL")
    ref = url["valueFrom"]["secretKeyRef"]
    credentials = one(
        obj["spec"]["target"]["template"]["data"]
        for obj in staging_manifests
        if obj["kind"] == "ExternalSecret"
        and obj["spec"].get("target", {}).get("name", obj["metadata"]["name"]) == ref["name"]
        and obj["metadata"]["namespace"] == namespace
    )
    database = find_resource(staging_manifests, "Database", "sandbox-service", namespace=namespace)
    uri = urlsplit(credentials[ref["key"]])
    assert uri.path == f"/{database['spec']['name']}" == "/sandbox_service"
    assert uri.username == database["spec"]["owner"]


@pytest.fixture(scope="module")
def rbac(staging_manifests: list[dict[str, Any]]) -> Rbac:
    return Rbac(staging_manifests)


@pytest.mark.parametrize(
    ("service", "group", "resource", "verbs", "exclusive"),
    [
        (app.NAME, GROUP, "sandboxes", {"get", "list", "watch"}, True),
        (app.NAME, GROUP, "sandboxtemplates", {"list"}, True),
        (app.NAME, "subresources.kubevirt.io", "virtualmachineinstances/console", {"get"}, False),
        (runner_gateway.NAME, GROUP, "sandboxes", {"get", "list", "watch", "patch"}, True),
        (runner_gateway.NAME, GROUP, "sandboxtemplates", set(), True),
        (sandbox_controller.NAME, GROUP, "sandboxes", {"create", "patch", "delete"}, False),
        (sandbox_controller.NAME, GROUP, "sandboxtemplates", {"get", "list"}, False),
    ],
)
def test_environment_management_authority(
    staging_manifests: list[dict[str, Any]],
    rbac: Rbac,
    service: str,
    group: str,
    resource: str,
    verbs: set[str],
    exclusive: bool,
) -> None:
    deployment = find_resource(staging_manifests, "Deployment", service)
    namespace = deployment["metadata"]["namespace"]
    account = deployment["spec"]["template"]["spec"]["serviceAccountName"]
    grants = rbac.identity("ServiceAccount", account, namespace)
    required = {Permission(namespace, group, resource, verb) for verb in verbs}
    assert not uncovered(required, grants)
    if exclusive:
        # Restrict each grant to the resource under test, retaining its scope and verb.
        matching = {
            permission
            for grant in grants
            if grant.covers(permission := Permission(grant.namespace, group, resource, grant.verb, grant.name))
        }
        assert not uncovered(matching, required)


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
