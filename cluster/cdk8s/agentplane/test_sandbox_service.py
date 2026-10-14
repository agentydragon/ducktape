"""The app owns operator writes; the Sandbox Service has no deployed resources."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import app, history_service, notifications, runner_gateway, session_dispatcher
from cluster.cdk8s.agentplane.conftest import NAMESPACES


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_sandbox_service_resources_are_absent(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    documents = agentplane_manifests[namespace]
    names = {document["metadata"]["name"] for document in documents}
    assert not any(name.startswith("agentplane-sandbox-service") for name in names)
    assert f"{namespace}-sandbox-service-token-reviewer" not in names

    history = _resource(documents, "Deployment", history_service.NAME)["spec"]["template"]["spec"]
    history_database = {"name": "postgres-sandbox-service", "key": "uri"}
    history_external_secret = _resource(documents, "ExternalSecret", "postgres-sandbox-service")
    assert history_external_secret["spec"]["target"]["name"] == "postgres-sandbox-service"
    history_database_resource = _resource(documents, "Database", "sandbox-service")
    assert history_database_resource["spec"]["name"] == "sandbox_service"
    assert history_database_resource["spec"]["owner"] == "sandbox_service"
    assert any(
        value.get("secretKeyRef") == history_database
        for container in [*history["initContainers"], *history["containers"]]
        for value in (item.get("valueFrom", {}) for item in container["env"])
    )


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_app_uses_direct_sandbox_access(namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]) -> None:
    documents = agentplane_manifests[namespace]
    application = _resource(documents, "Deployment", app.NAME)["spec"]["template"]["spec"]
    container = one(item for item in application["containers"] if item["name"] == "app")
    assert not any(arg.startswith("--sandbox-service-target=") for arg in container["args"])
    assert not any(volume["name"] == "sandbox-service-token" for volume in application["volumes"])

    app_rules = _resource(documents, "Role", app.NAME)["rules"]
    for rule in app_rules:
        if rule["resources"] not in (["policybindings"], ["sandboxes"]):
            assert set(rule["verbs"]) <= {"get", "list", "watch"}
    permissions = {(tuple(rule["apiGroups"]), tuple(rule["resources"]), tuple(rule["verbs"])) for rule in app_rules}
    assert (("agentplane.allegedly.works",), ("sandboxtemplates",), ("list",)) in permissions
    assert (("subresources.kubevirt.io",), ("virtualmachineinstances/console",), ("get",)) in permissions

    gateway = _resource(documents, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    assert gateway["serviceAccountName"] == runner_gateway.NAME


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_notifications_have_no_app_or_sandbox_service_dependency(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    documents = agentplane_manifests[namespace]
    pod = _resource(documents, "Deployment", notifications.NAME)["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == notifications.NAME
    tokens = one(volume for volume in pod["volumes"] if volume["name"] == "service-tokens")
    assert {source["serviceAccountToken"]["audience"] for source in tokens["projected"]["sources"]} == {
        "agentplane-egress",
        runner_gateway.TOKEN_AUDIENCE,
        history_service.TOKEN_AUDIENCE,
        session_dispatcher.TOKEN_AUDIENCE,
    }
    settings = yaml.safe_load(
        _resource(documents, "ConfigMap", f"{notifications.NAME}-settings")["data"]["settings.yaml"]
    )
    assert "sandbox_service" not in settings
    policy = _resource(documents, "CiliumNetworkPolicy", notifications.NAME)["spec"]
    assert not any(
        target["matchLabels"].get("app.kubernetes.io/name") == app.NAME
        for rule in policy["egress"]
        for target in rule.get("toEndpoints", [])
    )


if __name__ == "__main__":
    pytest_bazel.main()
