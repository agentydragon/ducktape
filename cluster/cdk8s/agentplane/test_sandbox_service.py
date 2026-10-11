"""Production routing/authority cutover must not leave a direct-runner fallback, and the Sandbox Service and
Runner Gateway run apart."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import app, history_service, notifications, runner_gateway, sandbox_service
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
    gateway_pod = resource("Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    assert gateway_pod["serviceAccountName"] == runner_gateway.NAME
    # The migration blocks startup; the gateway also needs the URL for explicit Open.
    commands_migration = one(gateway_pod["initContainers"])
    assert commands_migration["env"] == [
        {
            "name": "AGENTPLANE_SANDBOX_SERVICE_COMMANDS_DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"name": "postgres-sandbox-commands", "key": "uri"}},
        }
    ]
    gateway_container = one(item for item in gateway_pod["containers"] if item["name"] == "runner-gateway")
    assert commands_migration["env"][0]["valueFrom"] in [item.get("valueFrom") for item in gateway_container["env"]]
    # History belongs to the History Service, and command admission to the gateway: the Sandbox Service holds
    # no database credential.
    history_secret = {"secretKeyRef": {"name": "postgres-sandbox-service", "key": "uri"}}
    assert "initContainers" not in service_pod
    assert not any("valueFrom" in item for container in service_pod["containers"] for item in container.get("env", []))
    assert all(
        item.get("valueFrom") != history_secret
        for container in [*gateway_pod["initContainers"], *gateway_pod["containers"]]
        for item in container.get("env", [])
    )
    history_pod = resource("Deployment", history_service.NAME)["spec"]["template"]["spec"]
    history_migration = one(history_pod["initContainers"])
    assert history_migration["env"] == [
        {"name": "AGENTPLANE_SANDBOX_SERVICE_DATABASE_URL", "valueFrom": history_secret}
    ]
    for rule in resource("Role", app.NAME)["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}
    backend_config = yaml.safe_load(resource("ConfigMap", f"{sandbox_service.NAME}-config")["data"]["config.yaml"])
    gateway_config = yaml.safe_load(resource("ConfigMap", f"{runner_gateway.NAME}-config")["data"]["config.yaml"])
    # The Runner Gateway supplies this platform block to every harness/preset in both environments.
    platform = gateway_config["platform_instructions"]
    assert platform.count(runner_gateway.KUBERNETES_ADMIN_INSTRUCTIONS) == 1
    assert "kubernetes_admin" in platform
    assert "pods_exec" in platform
    assert f"http://agentplane-actions.{namespace}.svc.cluster.local:8080" in platform
    assert backend_config["kubernetes_binding_cleanup_namespaces"] == sorted(
        backend_config["kubernetes_binding_cleanup_namespaces"]
    )
    assert backend_config["caller_accounts"] == [
        {"namespace": namespace, "name": app.NAME},
        {"namespace": namespace, "name": notifications.NAME},
    ]
    assert gateway_config["caller_accounts"] == backend_config["caller_accounts"]
    # Only the Sandbox Service may seal a Sandbox, with the token it is configured to send.
    assert gateway_config["sealer_accounts"] == [{"namespace": namespace, "name": sandbox_service.NAME}]
    gateway_token = one(volume for volume in service_pod["volumes"] if volume["name"] == "runner-gateway-token")
    assert (
        one(gateway_token["projected"]["sources"])["serviceAccountToken"]["audience"]
        == gateway_config["token_audience"]
    )
    gateway_mount = one(
        mount
        for container in service_pod["containers"]
        for mount in container["volumeMounts"]
        if mount["name"] == "runner-gateway-token"
    )
    assert backend_config["gateway_token_file"] == f"{gateway_mount['mountPath']}/token"
    gateway = runner_gateway.service(namespace)
    assert backend_config["gateway_target"] == f"{gateway.fqdn}:{gateway.pod_port}"
    assert gateway_config["token_audience"] != backend_config["token_audience"]
    runner_policy = resource("CiliumNetworkPolicy", "agentplane-runner")["spec"]
    # Runners dial out; nothing connects to them.
    assert runner_policy["ingress"] == [{}]


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
        runner_gateway.TOKEN_AUDIENCE,
        history_service.TOKEN_AUDIENCE,
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
        target["matchLabels"].get("k8s:app.kubernetes.io/name") == app.NAME
        for rule in policy["egress"]
        for target in rule.get("toEndpoints", [])
    )


def _token_audience(pod: dict[str, Any], container: str, token_file: str) -> str:
    """The audience of the projected token a container finds at `token_file`."""
    mount = one(
        mount
        for item in pod["containers"]
        if item["name"] == container
        for mount in item["volumeMounts"]
        if token_file.startswith(f"{mount['mountPath']}/")
    )
    volume = one(volume for volume in pod["volumes"] if volume["name"] == mount["name"])
    audience: str = one(
        source["serviceAccountToken"]["audience"]
        for source in volume["projected"]["sources"]
        if source["serviceAccountToken"]["path"] == token_file.removeprefix(f"{mount['mountPath']}/")
    )
    return audience


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_gateway_callers_send_the_gateway_audience(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    documents = agentplane_manifests[namespace]

    def resource(kind: str, name: str) -> dict[str, Any]:
        return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)

    def pod(name: str) -> dict[str, Any]:
        spec: dict[str, Any] = resource("Deployment", name)["spec"]["template"]["spec"]
        return spec

    def config(name: str, key: str) -> dict[str, Any]:
        loaded: dict[str, Any] = yaml.safe_load(resource("ConfigMap", name)["data"][key])
        return loaded

    accepted = config(f"{runner_gateway.NAME}-config", "config.yaml")["token_audience"]
    app_args = one(item for item in pod(app.NAME)["containers"] if item["name"] == "app")["args"]
    app_token = one(
        arg.removeprefix("--runner-gateway-token-file=")
        for arg in app_args
        if arg.startswith("--runner-gateway-token-file=")
    )
    notices = config(f"{notifications.NAME}-settings", "settings.yaml")["sandbox_service"]
    sealer = config(f"{sandbox_service.NAME}-config", "config.yaml")
    assert {
        _token_audience(pod(app.NAME), "app", app_token),
        _token_audience(pod(notifications.NAME), "notifications", notices["gateway_token_file"]),
        _token_audience(pod(sandbox_service.NAME), "sandbox-service", sealer["gateway_token_file"]),
    } == {accepted}


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
