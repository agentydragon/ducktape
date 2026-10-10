"""Production routing/authority cutover must not leave a direct-runner fallback."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import app, history_service, notifications, sandbox_service
from cluster.cdk8s.agentplane.conftest import NAMESPACES


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_app_uses_independent_service(namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]) -> None:
    documents = agentplane_manifests[namespace]

    def resource(kind: str, name: str) -> dict[str, Any]:
        return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)

    application = resource("Deployment", app.NAME)["spec"]["template"]["spec"]
    container = one(item for item in application["containers"] if item["name"] == "app")
    target = sandbox_service.service(namespace)
    assert f"--sandbox-service-target={target.fqdn}:{target.pod_port}" in container["args"]
    assert not any(arg.startswith("--runner-port") for arg in container["args"])
    token = one(volume for volume in application["volumes"] if volume["name"] == "sandbox-service-token")
    assert token["projected"]["sources"] == [
        {
            "serviceAccountToken": {
                "audience": sandbox_service.TOKEN_AUDIENCE,
                "expirationSeconds": 3600,
                "path": "token",
            }
        }
    ]
    assert one(mount for mount in container["volumeMounts"] if mount["name"] == "sandbox-service-token")["readOnly"]
    history = history_service.service(namespace)
    assert f"--history-service-target={history.fqdn}:{history.pod_port}" in container["args"]
    history_token = one(volume for volume in application["volumes"] if volume["name"] == "history-service-token")
    assert history_token["projected"]["sources"] == [
        {
            "serviceAccountToken": {
                "audience": history_service.TOKEN_AUDIENCE,
                "expirationSeconds": 3600,
                "path": "token",
            }
        }
    ]
    assert one(mount for mount in container["volumeMounts"] if mount["name"] == "history-service-token")["readOnly"]
    service_pod = resource("Deployment", sandbox_service.NAME)["spec"]["template"]["spec"]
    assert service_pod["serviceAccountName"] == sandbox_service.NAME
    assert all("persistentVolumeClaim" not in volume for volume in service_pod.get("volumes", []))
    # The migrations block startup; the service also needs each URL for explicit Open.
    history_migration, commands_migration = service_pod["initContainers"]
    assert history_migration["env"] == [
        {
            "name": "AGENTPLANE_SANDBOX_SERVICE_DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"name": "postgres-sandbox-service", "key": "uri"}},
        }
    ]
    assert commands_migration["env"] == [
        {
            "name": "AGENTPLANE_SANDBOX_SERVICE_COMMANDS_DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"name": "postgres-sandbox-commands", "key": "uri"}},
        }
    ]
    service_container = one(item for item in service_pod["containers"] if item["name"] == "sandbox-service")
    assert history_migration["env"][0] in service_container["env"]
    assert commands_migration["env"][0] in service_container["env"]
    for rule in resource("Role", app.NAME)["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}
    backend_config = yaml.safe_load(resource("ConfigMap", f"{sandbox_service.NAME}-config")["data"]["config.yaml"])
    # The Sandbox Service supplies this platform block to every harness/preset in both environments.
    platform = backend_config["platform_instructions"]
    assert platform.count(sandbox_service.KUBERNETES_ADMIN_INSTRUCTIONS) == 1
    assert "kubernetes_admin" in platform
    assert "pods_exec" in platform
    assert f"http://agentplane-actions.{namespace}.svc.cluster.local:8080" in platform
    assert backend_config["kubernetes_binding_cleanup_namespaces"] == sorted(
        backend_config["kubernetes_binding_cleanup_namespaces"]
    )
    assert backend_config["caller_accounts"] == [
        {"namespace": namespace, "name": app.NAME},
        {"namespace": namespace, "name": history_service.NAME},
        {"namespace": namespace, "name": notifications.NAME},
    ]
    runner_policy = resource("CiliumNetworkPolicy", "agentplane-runner")["spec"]
    assert runner_policy["ingress"] == [
        {
            "fromEndpoints": [{"matchLabels": target.pods.cilium}],
            "toPorts": [{"ports": [{"port": "7000", "protocol": "TCP"}]}],
        }
    ]
    application_egress = resource("CiliumNetworkPolicy", app.NAME)["spec"]["egress"]
    assert not any(
        port["port"] == "7000"
        for rule in application_egress
        for ports in rule.get("toPorts", [])
        for port in ports["ports"]
    )


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_notifications_have_no_app_or_direct_runner_dependency(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    def resource(kind: str, name: str) -> dict[str, Any]:
        return one(
            doc for doc in agentplane_manifests[namespace] if doc["kind"] == kind and doc["metadata"]["name"] == name
        )

    pod = resource("Deployment", notifications.NAME)["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == notifications.NAME
    tokens = one(volume for volume in pod["volumes"] if volume["name"] == "service-tokens")
    assert {source["serviceAccountToken"]["audience"] for source in tokens["projected"]["sources"]} == {
        "agentplane-egress",
        sandbox_service.TOKEN_AUDIENCE,
    }
    for container in pod["containers"] + pod["initContainers"]:
        database = one(item for item in container["env"] if item["name"] == "AGENTPLANE_NOTIFICATIONS_DATABASE_URL")
        assert database["valueFrom"]["secretKeyRef"] == {"name": "postgres-notifications", "key": "uri"}
    role = resource("ClusterRole", f"{namespace}-notifications-token-reviewer")
    assert role["rules"] == [
        {"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]}
    ]
    policy = resource("CiliumNetworkPolicy", notifications.NAME)["spec"]
    assert not any(
        port["port"] == "7000"
        for rule in policy["egress"]
        for ports in rule.get("toPorts", [])
        for port in ports["ports"]
    )
    assert not any(
        target["matchLabels"].get("k8s:app.kubernetes.io/name") == app.NAME
        for rule in policy["egress"]
        for target in rule.get("toEndpoints", [])
    )


if __name__ == "__main__":
    pytest_bazel.main()


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_sandbox_reconciliation_can_watch_crs_and_read_owned_accounts(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    role = one(
        doc
        for doc in agentplane_manifests[namespace]
        if doc["kind"] == "Role" and doc["metadata"]["name"] == sandbox_service.NAME
    )
    rules = role["rules"]
    sandbox = one(
        rule for rule in rules if rule["apiGroups"] == ["agents.x-k8s.io"] and rule["resources"] == ["sandboxes"]
    )
    assert {"get", "list", "watch"} <= set(sandbox["verbs"])
    accounts = one(rule for rule in rules if rule["apiGroups"] == [""] and rule["resources"] == ["serviceaccounts"])
    assert {"create", "get", "patch", "delete"} <= set(accounts["verbs"])
    assert "secrets" not in accounts["resources"]
