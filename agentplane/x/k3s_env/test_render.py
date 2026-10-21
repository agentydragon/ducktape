"""The direct cdk8s fixture keeps the Go listeners and their authentication wired."""

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import bcrypt
import pytest_bazel
import yaml

from agentplane.action_service.oauth_settings import OAuthSettings
from agentplane.action_service.settings import Settings as ActionsSettings  # beside the runner gateway's
from agentplane.runner_gateway.settings import Settings
from agentplane.x.k3s_env import fixture_chart, fixture_database
from agentplane.x.k3s_env.rendered_fixture import McpInputs
from util.settings_contract import env_name

_MCP = McpInputs(
    dex_ca_cert="dex-ca\n", dex_cert="dex-cert\n", dex_key="dex-key\n", actions_origin="http://127.0.0.1:1"
)


def _resource(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    matches = [doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name]
    assert len(matches) == 1, (kind, name, len(matches))
    return matches[0]


def test_direct_fixture_uses_gateway_and_goose_queue_migration() -> None:
    docs = fixture_chart.objects(
        ca_cert="fixture-ca\n",
        ca_key="fixture-key\n",
        system_roots="system-roots\n",
        cluster_ca="cluster-ca\n",
        models=None,
        mcp=_MCP,
        vm_sandboxes=False,
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
        "egress": "agentplane-egress-history-migrate",
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
    egress_pod = _resource(docs, "Deployment", "egress")["spec"]["template"]["spec"]
    proxy = next(container for container in egress_pod["containers"] if container["name"] == "proxy")
    assert proxy["image"] == fixture_chart.EGRESS_IMAGE
    assert "args" not in proxy
    egress_config = yaml.safe_load(_resource(docs, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"])
    assert egress_config["admin_grpc_audience"] == "agentplane-egress-admin"
    assert egress_config["admin_grpc_callers"] == [f"{fixture_chart.NAMESPACE}/app"]
    app_pod = _resource(docs, "Deployment", "app")["spec"]["template"]["spec"]
    token = next(volume for volume in app_pod["volumes"] if volume["name"] == "egress-admin-token")
    assert token["projected"]["sources"][0]["serviceAccountToken"]["audience"] == "agentplane-egress-admin"
    assert any(
        mount["name"] == "egress-admin-token" and mount["mountPath"] == "/var/run/secrets/agentplane-egress-admin"
        for mount in app_pod["containers"][0]["volumeMounts"]
    )
    proxy_env = {entry["name"]: entry for entry in proxy["env"]}
    egress_migrator = next(container for container in egress_pod["initContainers"] if container["name"] == "migrate")
    assert egress_migrator["image"] == fixture_chart.EGRESS_MIGRATOR_IMAGE
    migrator_env = {entry["name"]: entry for entry in egress_migrator["env"]}
    assert migrator_env["DATABASE_URL"]["valueFrom"] == proxy_env["AGENTPLANE_EGRESS_DATABASE_URL"]["valueFrom"]
    assert "AGENTPLANE_EGRESS_DATABASE_URL" not in migrator_env
    assert proxy_env["PGSSLMODE"]["value"] == migrator_env["PGSSLMODE"]["value"] == "disable"
    for workload in ("history-service", "notifications"):
        pod_spec = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        for container in (*pod_spec["containers"], *pod_spec["initContainers"]):
            sslmode = {entry["name"]: entry.get("value") for entry in container.get("env", [])}.get("PGSSLMODE")
            assert sslmode == "disable", (workload, container["name"])

    gateway = _resource(docs, "Deployment", fixture_chart.RUNNER_GATEWAY)
    pod = gateway["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == fixture_chart.RUNNER_GATEWAY_IMAGE
    assert {port["name"]: port["containerPort"] for port in container["ports"]} == {"grpc": 8080, "http": 8081}
    database_env = next(
        entry for entry in container["env"] if entry["name"] == "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL"
    )
    migrator = next(entry for entry in pod["initContainers"] if entry["name"] == "goose-migrate")
    assert migrator["image"] == fixture_chart.QUEUE_MIGRATOR_IMAGE
    assert (
        next(entry for entry in migrator["env"] if entry["name"] == "DATABASE_URL")["valueFrom"]
        == database_env["valueFrom"]
    )
    token_mount = next(mount for mount in container["volumeMounts"] if mount["name"] == "runner-gateway-token")
    token = next(volume for volume in pod["volumes"] if volume["name"] == "runner-gateway-token")["projected"][
        "sources"
    ][0]["serviceAccountToken"]
    gateway_env = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert gateway_env[env_name(Settings, "runner_token_file")] == f"{token_mount['mountPath']}/{token['path']}"
    assert gateway_env[env_name(Settings, "gateway_audience")] == token["audience"]
    gateway_config = yaml.safe_load(
        _resource(docs, "ConfigMap", f"{fixture_chart.RUNNER_GATEWAY}-settings")["data"]["settings.yaml"]
    )
    assert f"{fixture_chart.NAMESPACE}/app" in gateway_config["wake_callers"]
    assert not pod.get("imagePullSecrets")

    for name, expected_port in (
        (fixture_chart.RUNNER_GATEWAY, 8080),
        (fixture_chart.SESSION_DISPATCHER, fixture_chart.DISPATCHER_PORT),
    ):
        service = _resource(docs, "Service", name)
        assert all(
            pod_label == gateway["spec"]["template"]["metadata"]["labels"][name]
            for name, pod_label in service["spec"]["selector"].items()
        )
        port = next(entry for entry in service["spec"]["ports"] if entry["name"] == "grpc")
        assert port["port"] == expected_port

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
    assert postgres_container["command"] == ["/bin/bash", "/fixture/postgres_entrypoint.sh"]
    assert postgres["spec"]["template"]["spec"]["securityContext"]["runAsUser"] == 26
    bootstrap_config = _resource(docs, "ConfigMap", "postgres-bootstrap")
    assert set(bootstrap_config["data"]) == {"bootstrap.sql", "postgres_entrypoint.sh"}
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

    # Dex accepts the operator's password: its hash is bcrypt of the password the Secret hands the test,
    # at no less than the cost Dex requires.
    operator = _resource(docs, "Secret", fixture_chart.OPERATOR_SECRET)["stringData"]
    (user,) = yaml.safe_load(operator["config.yaml"])["staticPasswords"]
    assert bcrypt.checkpw(operator["password"].encode(), user["hash"].encode())
    assert int(user["hash"].split("$")[2]) >= 10

    # The Dex clients' TLS verifies with a mounted bundle that holds the CA of Dex's certificate.
    for workload in ("app", "actions"):
        pod_spec = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        (container,) = pod_spec["containers"]
        cert_file = PurePosixPath(
            next(entry["value"] for entry in container["env"] if entry["name"] == "SSL_CERT_FILE")
        )
        mount = next(mount for mount in container["volumeMounts"] if mount["mountPath"] == str(cert_file.parent))
        volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == mount["name"])
        bundle = _resource(docs, "ConfigMap", volume["configMap"]["name"])["data"][cert_file.name]
        assert bundle.endswith(_MCP.dex_ca_cert), workload

    # The Action Service's OAuth server names itself by the forwarded origin and reads each of its files
    # from a mounted key of a Secret the fixture writes.
    actions_pod = _resource(docs, "Deployment", "actions")["spec"]["template"]["spec"]
    (actions_container,) = actions_pod["containers"]
    oauth = OAuthSettings.model_validate_json(
        next(
            entry["value"] for entry in actions_container["env"] if entry["name"] == env_name(ActionsSettings, "oauth")
        )
    )
    assert oauth.base_url == _MCP.actions_origin
    for path in (oauth.upstream_client_secret_file, oauth.jwt_signing_key_file, oauth.encryption_key_file):
        mount = next(mount for mount in actions_container["volumeMounts"] if mount["mountPath"] == str(path.parent))
        volume = next(volume for volume in actions_pod["volumes"] if volume["name"] == mount["name"])["secret"]
        assert {"key": path.name, "path": path.name} in volume["items"], path
        assert _resource(docs, "Secret", volume["secretName"])["stringData"][path.name], path


if __name__ == "__main__":
    pytest_bazel.main()
