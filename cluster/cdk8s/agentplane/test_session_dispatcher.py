"""The deployed Dispatcher is receiver-ready while senders remain on the Runner Gateway."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from agentplane.crd_group import GROUP
from cluster.cdk8s.agentplane import app, notifications, runner_gateway, session_dispatcher
from cluster.cdk8s.agentplane.conftest import NAMESPACES


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_dispatcher_deployment_and_gateway_auth(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    documents = agentplane_manifests[namespace]

    def resource(kind: str, name: str) -> dict[str, Any]:
        return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)

    deployment = resource("Deployment", session_dispatcher.NAME)
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert not any(
        doc["kind"] == "PodDisruptionBudget" and doc["metadata"]["name"] == session_dispatcher.NAME for doc in documents
    )
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == session_dispatcher.NAME
    migration = one(pod["initContainers"])
    container = one(item for item in pod["containers"] if item["name"] == "session-dispatcher")
    database_secret = {"secretKeyRef": {"name": "postgres-session-dispatcher", "key": "uri"}}
    database_env = {"name": "AGENTPLANE_SESSION_DISPATCHER_DATABASE_URL", "valueFrom": database_secret}
    assert migration["image"] == "git.allegedly.works/ducktape-ci/agentplane-session-dispatcher-migrate:unset"
    assert database_env in migration["env"]
    assert database_env in container["env"]
    assert container["image"] == "git.allegedly.works/ducktape-ci/agentplane-session-dispatcher:unset"

    settings = yaml.safe_load(resource("ConfigMap", f"{session_dispatcher.NAME}-config")["data"]["config.yaml"])
    assert settings == {
        "namespace": namespace,
        "caller_accounts": [
            {"namespace": namespace, "name": app.NAME},
            {"namespace": namespace, "name": notifications.NAME},
        ],
        "token_audience": session_dispatcher.TOKEN_AUDIENCE,
        "runner_control_target": runner_gateway.control_target(namespace),
        "runner_token_file": "/var/run/secrets/agentplane-runner-gateway/token",
        "port": 8080,
    }
    token_mount = one(mount for mount in container["volumeMounts"] if mount["name"] == "runner-gateway-token")
    assert token_mount["mountPath"] == "/var/run/secrets/agentplane-runner-gateway"
    assert token_mount["readOnly"] is True
    token_volume = one(volume for volume in pod["volumes"] if volume["name"] == "runner-gateway-token")
    assert token_volume["projected"]["sources"] == [
        {"serviceAccountToken": {"audience": runner_gateway.TOKEN_AUDIENCE, "expirationSeconds": 3600, "path": "token"}}
    ]

    database = resource("Database", "session-dispatcher")
    assert database["spec"]["name"] == "session_dispatcher"
    assert database["spec"]["owner"] == "session_dispatcher"
    assert database["spec"]["cluster"]["name"] == "postgres"
    service = resource("Service", session_dispatcher.NAME)
    assert service["spec"]["ports"] == [{"name": "grpc", "port": 8080, "protocol": "TCP", "targetPort": 8080}]

    role = resource("Role", session_dispatcher.NAME)
    assert role["rules"] == [{"apiGroups": [GROUP], "resources": ["sandboxes"], "verbs": ["get", "list", "watch"]}]
    role_binding = resource("RoleBinding", session_dispatcher.NAME)
    assert role_binding["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": session_dispatcher.NAME, "namespace": namespace}
    ]
    token_reviewer = resource("ClusterRole", f"{namespace}-session-dispatcher-token-reviewer")
    assert token_reviewer["rules"] == [
        {"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]}
    ]

    dispatcher_policy = resource("CiliumNetworkPolicy", session_dispatcher.NAME)["spec"]
    dispatcher_callers = {
        endpoint["matchLabels"]["app.kubernetes.io/name"]
        for rule in dispatcher_policy["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
    }
    assert dispatcher_callers == {app.NAME, notifications.NAME}
    dispatcher_egress = {
        endpoint["matchLabels"].get("app.kubernetes.io/name")
        for rule in dispatcher_policy["egress"]
        for endpoint in rule.get("toEndpoints", [])
    }
    assert runner_gateway.NAME in dispatcher_egress

    gateway_config = yaml.safe_load(resource("ConfigMap", f"{runner_gateway.NAME}-config")["data"]["config.yaml"])
    assert gateway_config["caller_accounts"] == [
        {"namespace": namespace, "name": app.NAME},
        {"namespace": namespace, "name": notifications.NAME},
        {"namespace": namespace, "name": session_dispatcher.NAME},
    ]
    gateway_policy = resource("CiliumNetworkPolicy", runner_gateway.NAME)["spec"]
    assert any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == session_dispatcher.NAME
        for rule in gateway_policy["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
    )


if __name__ == "__main__":
    pytest_bazel.main()
