"""Only the staging GitHub webhook is public; workload APIs stay behind egress auth."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import notifications, runner_gateway, session_dispatcher, staging
from cluster.cdk8s.agentplane.conftest import NAMESPACES


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_public_notification_routes_are_webhook_only(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    rules = [
        rule
        for doc in agentplane_manifests[namespace]
        if doc["kind"] == "HTTPRoute"
        for rule in doc["spec"]["rules"]
        if any(backend["name"] == notifications.NAME for backend in rule.get("backendRefs", []))
    ]
    if namespace != staging.ENV.namespace:
        assert not rules
        return
    rule = one(rules)
    assert rule["matches"] == [{"path": {"type": "Exact", "value": "/v1/webhooks/github"}}]
    assert all(filter_["type"] == "ResponseHeaderModifier" for filter_ in rule.get("filters", []))


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_only_staging_notifications_admit_gateway(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    endpoint = notifications.service(namespace)
    ingress = [
        rule
        for doc in agentplane_manifests[namespace]
        if doc["kind"] == "CiliumNetworkPolicy"
        and doc["spec"]["endpointSelector"].get("matchLabels") == endpoint.pods.selector
        for rule in doc["spec"].get("ingress", [])
        if "ingress" in rule.get("fromEntities", [])
    ]
    if namespace != staging.ENV.namespace:
        assert not ingress
        return
    assert one(ingress) == {
        "fromEntities": ["ingress"],
        "toPorts": [{"ports": [{"port": str(endpoint.pod_port), "protocol": "TCP"}]}],
    }


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_github_secrets_are_server_only_and_not_in_yaml(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    documents = agentplane_manifests[namespace]
    config = one(
        doc
        for doc in documents
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == f"{notifications.NAME}-settings"
    )
    github = yaml.safe_load(config["data"]["settings.yaml"]).get("github")
    pod = one(
        doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == notifications.NAME
    )["spec"]["template"]["spec"]
    prefix = "AGENTPLANE_NOTIFICATIONS_GITHUB__"
    for container in pod["initContainers"]:
        assert not any(item["name"].startswith(prefix) for item in container["env"])
    server = one(item for item in pod["containers"] if item["name"] == "notifications")
    variables = {item["name"].removeprefix(prefix): item for item in server["env"] if item["name"].startswith(prefix)}
    if namespace != staging.ENV.namespace:
        assert github is None
        assert not variables
        return
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


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_notifications_use_the_configured_dispatcher(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    documents = agentplane_manifests[namespace]
    config = one(
        doc
        for doc in documents
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == f"{notifications.NAME}-settings"
    )
    settings = yaml.safe_load(config["data"]["settings.yaml"])
    dispatcher = session_dispatcher.service(namespace)
    assert settings["session_dispatcher"] == {
        "target": f"{dispatcher.fqdn}:{dispatcher.port.number}",
        "token_file": "/var/run/secrets/notifications/dispatcher",
    }

    deployment = one(
        doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == notifications.NAME
    )
    pod = deployment["spec"]["template"]["spec"]
    token_volume = one(volume for volume in pod["volumes"] if volume["name"] == "service-tokens")
    tokens = {
        source["serviceAccountToken"]["path"]: source["serviceAccountToken"]
        for source in token_volume["projected"]["sources"]
    }
    assert tokens["dispatcher"]["audience"] == session_dispatcher.TOKEN_AUDIENCE

    policy = one(
        doc
        for doc in documents
        if doc["kind"] == "CiliumNetworkPolicy" and doc["metadata"]["name"] == notifications.NAME
    )["spec"]
    egress_services = {
        endpoint["matchLabels"].get("app.kubernetes.io/name")
        for rule in policy["egress"]
        for endpoint in rule.get("toEndpoints", [])
    }
    assert {dispatcher.NAME, runner_gateway.NAME} <= egress_services


if __name__ == "__main__":
    pytest_bazel.main()
