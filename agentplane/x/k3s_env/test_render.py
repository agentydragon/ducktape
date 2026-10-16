"""The direct cdk8s fixture keeps the Go listeners and their authentication wired."""

from typing import Any
from urllib.parse import urlsplit

import pytest_bazel

from agentplane.x.k3s_env import fixture_chart, fixture_database


def _resource(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    matches = [doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name]
    assert len(matches) == 1, (kind, name, len(matches))
    return matches[0]


def test_direct_fixture_uses_go_gateway_and_goose_queue_migration() -> None:
    docs = fixture_chart.objects(
        node_ip="192.0.2.20",
        ca_cert="fixture-ca\n",
        ca_key="fixture-key\n",
        system_roots="system-roots\n",
        cluster_ca="cluster-ca\n",
        litellm_key=None,
    )
    assert all(
        doc["kind"]
        not in {
            "CiliumNetworkPolicy",
            "HTTPRoute",
            "ExternalSecret",
            "Password",
            "Bundle",
            "Certificate",
            "ServiceMonitor",
            "PrometheusRule",
            "ResourceQuota",
            "LimitRange",
        }
        for doc in docs
    )
    assert not any(
        doc["kind"] == "Deployment" and doc["metadata"]["name"] == fixture_chart.SESSION_DISPATCHER for doc in docs
    )
    assert not any(doc["kind"] == "Deployment" and doc["metadata"]["name"] == "electric" for doc in docs)
    for workload, migration in {
        "actions": "agentplane-action-service-migrate",
        "app": "agentplane-app-migrate",
        "egress": "agentplane-egress-migrate",
        "history-service": "agentplane-history-migrate",
        "notifications": "agentplane-notification-service-migrate",
    }.items():
        pod_spec = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        init = next(container for container in pod_spec["initContainers"] if container["name"] == "migrate")
        assert init["image"] == f"git.allegedly.works/ducktape-ci/{migration}:unset"
        assert init["imagePullPolicy"] == "IfNotPresent"
    assert (
        _resource(docs, "Deployment", "actions")["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"]
        == "IfNotPresent"
    )
    for service in (doc for doc in docs if doc["kind"] == "Service"):
        assert service["spec"]["clusterIP"] == "None", service["metadata"]["name"]
        for port in service["spec"]["ports"]:
            assert port["port"] == port["targetPort"], service["metadata"]["name"]

    gateway = _resource(docs, "Deployment", fixture_chart.RUNNER_GATEWAY)
    pod = gateway["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == fixture_chart.GO_RUNNER_GATEWAY_IMAGE
    assert {port["name"]: port["containerPort"] for port in container["ports"]} == {"grpc": 8080}
    database_env = next(
        entry for entry in container["env"] if entry["name"] == "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL"
    )
    migrator = next(entry for entry in pod["initContainers"] if entry["name"] == "goose-migrate")
    assert migrator["image"] == fixture_chart.GO_QUEUE_MIGRATOR_IMAGE
    assert (
        next(entry for entry in migrator["env"] if entry["name"] == "DATABASE_URL")["valueFrom"]
        == database_env["valueFrom"]
    )
    token_mount = next(mount for mount in container["volumeMounts"] if mount["name"] == "runner-gateway-token")
    token = next(volume for volume in pod["volumes"] if volume["name"] == "runner-gateway-token")["projected"][
        "sources"
    ][0]["serviceAccountToken"]
    args = set(container["args"])
    assert f"--runner-token-file={token_mount['mountPath']}/{token['path']}" in args
    assert f"--gateway-audience={token['audience']}" in args
    assert f"--wake-caller={fixture_chart.NAMESPACE}/app" in args
    assert not pod.get("imagePullSecrets")

    for name, expected_port in (
        (fixture_chart.RUNNER_GATEWAY, 8080),
        (fixture_chart.SESSION_DISPATCHER, fixture_chart.DISPATCHER_PORT),
    ):
        service = _resource(docs, "Service", name)
        assert service["spec"]["clusterIP"] == "None"
        assert all(
            pod_label == gateway["spec"]["template"]["metadata"]["labels"][name]
            for name, pod_label in service["spec"]["selector"].items()
        )
        port = next(entry for entry in service["spec"]["ports"] if entry["name"] == "grpc")
        assert port["port"] == port["targetPort"] == expected_port

    endpoint = f"{fixture_chart.SESSION_DISPATCHER}.{fixture_chart.NAMESPACE}.svc.cluster.local:{fixture_chart.DISPATCHER_PORT}"
    app = _resource(docs, "Deployment", "app")
    assert not any(
        entry["name"] == "AGENTPLANE_ELECTRIC_URL" for entry in app["spec"]["template"]["spec"]["containers"][0]["env"]
    )
    assert f"--session-dispatcher-target={endpoint}" in app["spec"]["template"]["spec"]["containers"][0]["args"]
    app_env = app["spec"]["template"]["spec"]["containers"][0]["env"]
    assert any(
        entry["name"].endswith("RUNNER_GATEWAY_CONTROL_TARGET")
        and entry["value"] == "{sandbox}-control.{namespace}.svc:8080"
        for entry in app_env
    )
    notifications = _resource(docs, "ConfigMap", "notifications-settings")
    assert endpoint in notifications["data"]["settings.yaml"]
    role = _resource(docs, "Role", fixture_chart.RUNNER_GATEWAY)
    assert any("sandboxes" in rule["resources"] and "patch" in rule["verbs"] for rule in role["rules"])
    assert _resource(docs, "ClusterRole", f"{fixture_chart.NAMESPACE}-runner-gateway-token-reviewer")["rules"] == [
        {"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]}
    ]
    postgres = _resource(docs, "Deployment", fixture_database.NAME)
    postgres_container = next(
        container for container in postgres["spec"]["template"]["spec"]["containers"] if container["name"] == "postgres"
    )
    assert postgres_container["image"] == fixture_database.IMAGE
    postgres_service = _resource(docs, "Service", fixture_database.SERVICE)
    assert postgres_service["spec"]["clusterIP"] == "None"
    assert not any(doc["kind"] in {"Cluster", "Database"} for doc in docs)

    bootstrap_env = {entry["name"]: entry for entry in postgres_container["env"]}
    bootstrap_secret = _resource(docs, "Secret", "postgres-bootstrap-credentials")
    assert bootstrap_env["POSTGRES_USER"]["value"] == "fixture_admin"
    assert (
        bootstrap_env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == bootstrap_secret["metadata"]["name"]
    )

    database_roles = {
        "actions": "actions",
        "egress": "egress",
        "notifications": "notifications",
        "sandbox_service": "sandbox_service",
        "session_dispatcher": "session_dispatcher",
    }
    expected_secret_names = {role: f"postgres-{role.replace('_', '-')}" for role in database_roles}
    for role_name, database_name in database_roles.items():
        secret = _resource(docs, "Secret", expected_secret_names[role_name])
        data = secret["stringData"]
        uri = urlsplit(data["uri"])
        assert uri.scheme == "postgresql"
        assert uri.username == role_name
        assert uri.password == data["password"]
        assert uri.hostname == f"{fixture_database.SERVICE}.{fixture_chart.NAMESPACE}.svc"
        assert uri.port == 5432
        assert uri.path == f"/{database_name}"

    app_secret = _resource(docs, "Secret", "postgres-app")
    app_data = app_secret["stringData"]
    app_uri = urlsplit(app_data["async-uri"])
    assert app_uri.scheme == "postgresql+asyncpg"
    assert app_uri.username == "app"
    assert app_uri.password == app_data["password"]
    assert app_uri.hostname == f"{fixture_database.SERVICE}.{fixture_chart.NAMESPACE}.svc"
    assert app_uri.port == 5432
    assert app_uri.path == "/app"
    assert not any(doc["kind"] == "Secret" and doc["metadata"]["name"] == "postgres-electric" for doc in docs)

    template = _resource(docs, "SandboxTemplate", "runner")
    assert template["spec"]["stateVolume"]["claim"]["storageClassName"] == "local-path"

    for name, expected_image in (
        ("sandbox", "git.allegedly.works/ducktape-ci/agentplane-sandbox:unset"),
        ("sandbox-build", "git.allegedly.works/ducktape-ci/agentplane-sandbox-build:unset"),
    ):
        pod_template = _resource(docs, "PodTemplate", name)
        assert pod_template["apiVersion"] == "v1"
        pod = pod_template["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert not pod.get("imagePullSecrets")
        containers = {container["name"]: container for container in pod["containers"]}
        assert containers["sandbox"]["image"] == expected_image
        assert containers["egress-sidecar"]["image"] == (
            "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar:unset"
        )
    build_pod = _resource(docs, "PodTemplate", "sandbox-build")["template"]["spec"]
    home = next(volume for volume in build_pod["volumes"] if volume["name"] == "home")
    assert home["emptyDir"]["sizeLimit"] == "20Gi"


if __name__ == "__main__":
    pytest_bazel.main()
