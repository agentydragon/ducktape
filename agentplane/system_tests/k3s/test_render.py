"""The direct cdk8s fixture keeps the Go listeners and their authentication wired."""

import json
import os
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import bcrypt
import pytest_bazel
import yaml
from pydantic import AnyHttpUrl

# Each service module exports `Settings`; aliases keep their separate config contracts explicit.
from agentplane.app.settings import Settings as AppSettings
from agentplane.mcp_edge.oauth_settings import OAuthSettings
from agentplane.mcp_edge.settings import Settings as McpEdgeSettings
from agentplane.runner_gateway.settings import Settings
from agentplane.system_tests.k3s import fixture_chart, fixture_database, rendered_fixture
from agentplane.system_tests.k3s.block_storage import STORAGE_CLASS
from agentplane.system_tests.k3s.namespaces import NAMESPACE
from agentplane.system_tests.k3s.rendered_fixture import LocalModel, McpInputs, RuntimeInputs
from util.bazel.runfiles import get_required_path
from util.settings_contract import env_name

_MCP = McpInputs(
    dex_ca_cert="dex-ca\n",
    dex_cert="dex-cert\n",
    dex_key="dex-key\n",
    mcp_edge_origin="http://127.0.0.1:1",
    app_origin="http://127.0.0.1:2",
)


def _resource(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    matches = [doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name]
    assert len(matches) == 1, (kind, name, len(matches))
    return matches[0]


def _filled_fixture() -> rendered_fixture.RenderedFixture:
    cached = rendered_fixture.read(get_required_path(os.environ["K3S_FIXTURE_RLOCATION"]))
    return rendered_fixture.fill(
        cached,
        RuntimeInputs(
            ca_cert="fixture-ca\n",
            ca_key="fixture-key\n",
            system_roots="system-roots\n",
            cluster_ca="cluster-ca\n",
            runner_ca_cert="runner-ca\n",
        ),
        LocalModel(url="http://scripted-model.k3s-fixture.invalid:4000"),
        _MCP,
    )


def test_direct_fixture_uses_gateway_and_goose_queue_migration() -> None:
    fixture = _filled_fixture()
    assert "k3s-env-placeholder-" not in fixture.model_dump_json()
    docs = fixture.objects
    assert all(
        doc["kind"]
        not in {
            "CiliumNetworkPolicy",
            "HTTPRoute",
            "ExternalSecret",
            "Password",
            "Bundle",
            "ServiceMonitor",
            "PrometheusRule",
            "ResourceQuota",
            "LimitRange",
        }
        for doc in docs
    )
    assert {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Certificate"} == {
        "agentplane-runner-ca",
        "agentplane-runner-gateway",
    }
    assert _resource(docs, "Issuer", "agentplane-runner-ca")["spec"]["ca"]["secretName"] == "agentplane-runner-ca"
    gateway_certificate = _resource(docs, "Certificate", "agentplane-runner-gateway")
    assert gateway_certificate["spec"]["uris"] == ["spiffe://agentplane/runner-gateway"]
    assert set(gateway_certificate["spec"]["usages"]) >= {"client auth", "server auth"}
    trust_bundle = _resource(docs, "ConfigMap", "cluster-internal-ca-bundle")
    assert trust_bundle["data"]["ca-certificates.crt"] == "runner-ca\n"
    assert not any(
        doc["kind"] == "Deployment" and doc["metadata"]["name"] == fixture_chart.SESSION_DISPATCHER for doc in docs
    )
    assert not any(doc["kind"] == "Deployment" and doc["metadata"]["name"] == "electric" for doc in docs)
    for workload, migration in {
        "actions": "agentplane-action-service",
        "app": "agentplane-app-migrate",
        "egress": "agentplane-egress-history-migrate",
        "history-service": "agentplane-history-migrate",
        "mcp-edge": "agentplane-mcp-edge-migrate",
        "notifications": "agentplane-notification-service",
        "runner-gateway": "agentplane-runner-gateway-queue-migrate",
    }.items():
        pod_spec = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        init = next(container for container in pod_spec["initContainers"] if container["name"] == "migrate")
        assert init["image"] == f"git.allegedly.works/ducktape-ci/{migration}:unset"
        assert init["imagePullPolicy"] == "IfNotPresent"
        if workload == "actions":
            assert init["command"] == ["/usr/local/bin/actions-migrate"]
    assert (
        _resource(docs, "Deployment", "actions")["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"]
        == "IfNotPresent"
    )
    actions_container = _resource(docs, "Deployment", "actions")["spec"]["template"]["spec"]["containers"][0]
    assert actions_container["readinessProbe"]["grpc"] == {"port": fixture_chart.ACTIONS_SERVICE.pod_port}
    assert actions_container["livenessProbe"]["grpc"] == {
        "port": fixture_chart.ACTIONS_SERVICE.pod_port,
        "service": "livez",
    }
    edge_pod = _resource(docs, "Deployment", "mcp-edge")["spec"]["template"]["spec"]
    edge_container = edge_pod["containers"][0]
    edge_config = yaml.safe_load(_resource(docs, "ConfigMap", "mcp-edge-settings")["data"]["settings.yaml"])
    assert edge_config["actions_target"] == (
        f"{fixture_chart.ACTIONS_SERVICE.fqdn}:{fixture_chart.ACTIONS_SERVICE.port.number}"
    )
    app_container = _resource(docs, "Deployment", "app")["spec"]["template"]["spec"]["containers"][0]
    app_env = {entry["name"]: entry for entry in app_container["env"]}
    assert AnyHttpUrl(app_env[env_name(AppSettings, "connection_service_url")]["value"]) == AnyHttpUrl(
        fixture_chart.MCP_EDGE_SERVICE.url
    )
    assert "AGENTPLANE_ACTIONS_OAUTH" not in {entry["name"] for entry in actions_container["env"]}
    actions_config = yaml.safe_load(_resource(docs, "ConfigMap", "actions-settings")["data"]["settings.yaml"])
    assert actions_config["integrated_connection_service"] is False
    edge_database_env_name = env_name(McpEdgeSettings, "database_url")
    edge_database_env = {entry["name"]: entry for entry in edge_container["env"]}[edge_database_env_name]
    edge_migrator = edge_pod["initContainers"][0]
    edge_migrator_env = {entry["name"]: entry for entry in edge_migrator["env"]}[edge_database_env_name]
    assert edge_database_env["valueFrom"] == edge_migrator_env["valueFrom"]
    assert edge_database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-mcp-edge"
    egress_pod = _resource(docs, "Deployment", "egress")["spec"]["template"]["spec"]
    proxy = next(container for container in egress_pod["containers"] if container["name"] == "proxy")
    assert proxy["image"] == fixture_chart.EGRESS_IMAGE
    assert "args" not in proxy
    egress_config = yaml.safe_load(_resource(docs, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"])
    assert egress_config["admin_grpc_audience"] == "agentplane-egress-admin"
    assert egress_config["admin_grpc_callers"] == [f"{NAMESPACE}/app"]
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
    assert {port["name"]: port["containerPort"] for port in container["ports"]} == {
        "grpc": 8080,
        "http": 8081,
        "runner-control": 8090,
    }
    database_env = next(
        entry for entry in container["env"] if entry["name"] == "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL"
    )
    migrator = next(entry for entry in pod["initContainers"] if entry["name"] == "migrate")
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
    assert f"{NAMESPACE}/app" in gateway_config["wake_callers"]
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

    endpoint = f"{fixture_chart.SESSION_DISPATCHER}.{NAMESPACE}.svc.cluster.local:{fixture_chart.DISPATCHER_PORT}"
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
    assert _resource(docs, "ClusterRole", f"{NAMESPACE}-runner-gateway-token-reviewer")["rules"] == [
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
        "mcp_edge": "mcp_edge",
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
        assert uri.hostname == f"{fixture_database.SERVICE}.{NAMESPACE}.svc"
        assert uri.port == 5432
        assert uri.path == f"/{database_name}"

    app_secret = _resource(docs, "Secret", "postgres-app")
    app_data = app_secret["stringData"]
    app_uri = urlsplit(app_data["async-uri"])
    assert app_uri.scheme == "postgresql+asyncpg"
    assert app_uri.username == "app"
    assert app_uri.password == app_data["password"]
    assert app_uri.hostname == f"{fixture_database.SERVICE}.{NAMESPACE}.svc"
    assert app_uri.port == 5432
    assert app_uri.path == "/app"
    assert not any(doc["kind"] == "Secret" and doc["metadata"]["name"] == "postgres-electric" for doc in docs)

    # The fixture's one Pod execution environment, on its local-path storage.
    template = _resource(docs, "SandboxTemplate", fixture_chart.POD_TEMPLATE)
    assert template["spec"]["stateVolume"]["claim"]["storageClassName"] == "local-path"
    pod_containers = {container["name"]: container for container in template["spec"]["pod"]["spec"]["containers"]}
    runner_env = {entry["name"]: entry.get("value") for entry in pod_containers["runner"]["env"]}
    assert runner_env["OTEL_SDK_DISABLED"] == "true"
    sidecar_env = {entry["name"]: entry.get("value") for entry in pod_containers["egress-sidecar"]["env"]}
    assert sidecar_env["OTEL_SDK_DISABLED"] == "true"
    assert "OTEL_SDK_DISABLED" not in {entry["name"] for entry in pod_containers["codex-executor"].get("env", [])}
    executor = pod_containers["codex-executor"]
    assert executor["livenessProbe"]["initialDelaySeconds"] == 5
    assert executor["livenessProbe"]["periodSeconds"] == 5
    assert executor["readinessProbe"] == {
        "httpGet": {"path": "/healthz", "port": 4502},
        "periodSeconds": 2,
        "timeoutSeconds": 2,
        "failureThreshold": 1,
    }

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
    assert fixture.mcp_fixtures is not None
    operator = _resource(docs, "Secret", fixture.mcp_fixtures.operator_secret)["stringData"]
    (user,) = yaml.safe_load(operator["config.yaml"])["staticPasswords"]
    assert bcrypt.checkpw(operator["password"].encode(), user["hash"].encode())
    assert int(user["hash"].split("$")[2]) >= 10

    # The Dex clients' TLS verifies with a mounted bundle that holds the CA of Dex's certificate.
    for workload in ("app", "actions", "mcp-edge"):
        pod_spec = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        (container,) = pod_spec["containers"]
        cert_file = PurePosixPath(
            next(entry["value"] for entry in container["env"] if entry["name"] == "SSL_CERT_FILE")
        )
        mount = next(mount for mount in container["volumeMounts"] if mount["mountPath"] == str(cert_file.parent))
        volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == mount["name"])
        bundle = _resource(docs, "ConfigMap", volume["configMap"]["name"])["data"][cert_file.name]
        assert bundle.endswith(_MCP.dex_ca_cert), workload

    # The standalone edge names itself by the forwarded origin and reads its upstream client secret
    # from its own Dex client Secret. Actions retains only its distinct upstream MCP linkage client.
    edge_oauth_env = next(
        entry for entry in edge_container["env"] if entry["name"] == env_name(McpEdgeSettings, "oauth")
    )
    edge_oauth_secret_ref = edge_oauth_env["valueFrom"]["secretKeyRef"]
    assert edge_oauth_secret_ref["key"] == "oauth"
    edge_secret = _resource(docs, "Secret", edge_oauth_secret_ref["name"])["stringData"]
    edge_oauth = OAuthSettings.model_validate_json(edge_secret["oauth"])
    assert edge_oauth.base_url == _MCP.mcp_edge_origin
    assert edge_secret["client-secret"]
    edge_secret_mount = next(
        mount for mount in edge_container["volumeMounts"] if mount["mountPath"] == "/etc/agentplane-mcp"
    )
    edge_secret_volume = next(volume for volume in edge_pod["volumes"] if volume["name"] == edge_secret_mount["name"])[
        "secret"
    ]
    assert edge_secret_volume["secretName"] == edge_oauth_secret_ref["name"]
    assert {"key": "client-secret", "path": "client-secret"} in edge_secret_volume["items"]
    assert _resource(docs, "Secret", edge_secret_volume["secretName"])["stringData"]["client-secret"]
    operator_config = yaml.safe_load(operator["config.yaml"])
    clients = {client["id"]: client for client in operator_config["staticClients"]}
    assert clients[edge_oauth.upstream_client_id]["redirectURIs"] == [f"{_MCP.mcp_edge_origin}/auth/callback"]
    assert clients["test-mcp-linkage"]["redirectURIs"] == [f"{_MCP.app_origin}/mcp-linkage/callback"]
    mintable_accounts = edge_config["mintable_accounts"]
    assert mintable_accounts
    for account in mintable_accounts:
        namespace, name = account.split("/")
        caller = _resource(docs, "ServiceAccount", name)
        assert caller["metadata"]["namespace"] == namespace
        assert caller["metadata"]["labels"]["agentplane.allegedly.works/use-action-service"] == "true"
    token_rules = [
        rule for rule in _resource(docs, "Role", "mcp-edge")["rules"] if "serviceaccounts/token" in rule["resources"]
    ]
    assert {name for rule in token_rules for name in rule["resourceNames"]} == {
        account.split("/")[1] for account in mintable_accounts
    }


def test_vm_fixture_uses_raw_block_workspace_and_filesystem_state() -> None:
    fixture = rendered_fixture.read(get_required_path(os.environ["K3S_VM_FIXTURE_RLOCATION"]))
    template = _resource(fixture.objects, "SandboxTemplate", "runner-vm")["spec"]
    state = template["stateVolume"]
    workspace = template["workspaceVolume"]

    assert state["claim"]["storageClassName"] == "local-path"
    assert state["claim"].get("volumeMode", "Filesystem") == "Filesystem"
    assert workspace["layout"] == {"type": "BlockFilesystem", "filesystem": "ext4"}
    assert workspace["claim"]["storageClassName"] == STORAGE_CLASS
    assert workspace["claim"]["volumeMode"] == "Block"

    forward = _resource(fixture.objects, "SandboxTemplate", fixture_chart.FORWARD_VM_TEMPLATE)["spec"]
    assert forward["runnerTransport"] == "forward"
    vm_spec = forward["vm"]["template"]["spec"]
    interface = next(item for item in vm_spec["domain"]["devices"]["interfaces"] if item["name"] == "default")
    assert {"port": 9090} in interface["ports"]
    config_name = next(volume["configMap"]["name"] for volume in vm_spec["volumes"] if "configMap" in volume)
    config_map = _resource(fixture.objects, "ConfigMap", config_name)
    config = json.loads(config_map["data"]["runner.json"])
    assert config["control"]["mode"] == "forward"
    assert config["control"]["listen_address"] == "0.0.0.0:9090"


if __name__ == "__main__":
    pytest_bazel.main()
