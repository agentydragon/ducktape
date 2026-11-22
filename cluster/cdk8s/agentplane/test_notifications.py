"""The service HTTP listener is webhook-only; workload APIs use its gRPC listener."""

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one
from python.runfiles import Runfiles

from agentplane.net.peertls.config import APP_URI
from cluster.cdk8s.agentplane import actions as action_service, notifications, runner_gateway, staging
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource


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


def test_github_credentials_reference_the_staging_secret(
    staging_manifests: list[dict[str, Any]], runfiles: Runfiles
) -> None:
    secret = yaml.safe_load(
        (runfiles.root() / "ducktape/cluster/k8s/agentplane-staging/github-app.sops.yaml").read_text()
    )
    deployment = find_resource(staging_manifests, "Deployment", notifications.NAME)
    assert deployment["metadata"]["namespace"] == secret["metadata"]["namespace"]
    server = one(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == notifications.NAME)
    for variable, key in [
        ("AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY", "private-key"),
        ("AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET", "webhook-secret"),
    ]:
        env = one(item for item in server["env"] if item["name"] == variable)
        ref = env["valueFrom"]["secretKeyRef"]
        assert ref["name"] == secret["metadata"]["name"]
        assert ref["key"] == key
        assert key in secret["stringData"]
        assert not ref.get("optional", False)


def test_notifications_use_the_configured_backend_targets(staging_manifests: list[dict[str, Any]]) -> None:
    settings = config_map_yaml(staging_manifests, f"{notifications.NAME}-settings", "settings.yaml")
    assert settings["operator_reader_services"] == [APP_URI]
    assert [auth["mode"] for auth in settings["rpc"]["auth"]] == ["spiffe"]
    actions = action_service.service(staging.ENV.namespace)
    assert settings["actions"]["url"] == f"https://{actions.authority}/"
    dispatcher = runner_gateway.sessions_service(staging.ENV.namespace)
    assert settings["session_dispatcher"]["target"] == dispatcher.authority
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
