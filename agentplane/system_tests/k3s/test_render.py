"""The direct cdk8s fixture keeps the Go listeners and their authentication wired."""

import json
import os
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import bcrypt
import pytest
import pytest_bazel
import yaml
from pydantic import AnyHttpUrl, ValidationError

from agentplane.app.model_catalog import ModelCatalog

# Each service module exports `Settings`; aliases keep their separate config contracts explicit.
from agentplane.app.settings import Settings as AppSettings
from agentplane.mcp_edge.oauth_settings import OAuthSettings
from agentplane.mcp_edge.settings import Settings as McpEdgeSettings
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.settings import Settings
from agentplane.system_tests.k3s import fixture_chart, fixture_database, rendered_fixture
from agentplane.system_tests.k3s.block_storage import STORAGE_CLASS
from agentplane.system_tests.k3s.namespaces import NAMESPACE
from agentplane.system_tests.k3s.rendered_fixture import LocalModel, McpInputs, RuntimeInputs
from agentplane.system_tests.vm_configuration import SUPERVISION_KERNEL_ARG, SUPERVISION_TEMPLATE
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from agentplane.x.central_sampler.settings import (
    MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
    Settings as CentralSamplerSettings,
)
from cluster.cdk8s.agentplane import central_sampler, history_service, llm_ingress, runner_gateway
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
            ca_cert="fixture-ca\n", ca_key="fixture-key\n", system_roots="system-roots\n", cluster_ca="cluster-ca\n"
        ),
        LocalModel(url="http://scripted-model.k3s-fixture.invalid:4000"),
        _MCP,
    )


def _filled_vm_fixture() -> rendered_fixture.RenderedFixture:
    cached = rendered_fixture.read(get_required_path(os.environ["K3S_VM_FIXTURE_RLOCATION"]))
    return rendered_fixture.fill(
        cached,
        RuntimeInputs(
            ca_cert="fixture-ca\n", ca_key="fixture-key\n", system_roots="system-roots\n", cluster_ca="cluster-ca\n"
        ),
        LocalModel(url="http://scripted-model.k3s-fixture.invalid:4000"),
        None,
    )


def test_direct_fixture_uses_gateway_and_goose_queue_migration() -> None:
    fixture = _filled_fixture()
    assert fixture.central_sampler
    assert "k3s-env-placeholder-" not in fixture.model_dump_json()
    docs = fixture.objects
    assert all(
        doc["kind"]
        not in {
            "CiliumNetworkPolicy",
            "HTTPRoute",
            "ExternalSecret",
            "Password",
            "ServiceMonitor",
            "PrometheusRule",
            "ResourceQuota",
            "LimitRange",
        }
        for doc in docs
    )
    assert {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Certificate"} == {
        "test-runner-root",
        "agentplane-runner-ca",
        "agentplane-runner-gateway",
    }
    assert _resource(docs, "Issuer", "agentplane-runner-ca")["spec"]["ca"]["secretName"] == "agentplane-runner-ca"
    gateway_certificate = _resource(docs, "Certificate", "agentplane-runner-gateway")
    assert gateway_certificate["spec"]["uris"] == ["spiffe://agentplane/runner-gateway"]
    assert set(gateway_certificate["spec"]["usages"]) == {"digital signature", "client auth"}
    trust_bundle = _resource(docs, "Bundle", "cluster-internal-ca-bundle")
    root = _resource(docs, "Certificate", "test-runner-root")
    assert trust_bundle["spec"]["sources"] == [{"secret": {"name": root["spec"]["secretName"], "key": "ca.crt"}}]
    assert trust_bundle["spec"]["target"]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": fixture.namespace
    }
    assert not any(
        doc["kind"] == "Deployment" and doc["metadata"]["name"] == fixture_chart.SESSION_DISPATCHER for doc in docs
    )
    assert not any(doc["kind"] == "Deployment" and doc["metadata"]["name"] == "electric" for doc in docs)
    for workload, migration in {
        "actions": "agentplane-action-service",
        "notifications": "agentplane-notification-service",
    }.items():
        pod_spec = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        init = next(container for container in pod_spec["initContainers"] if container["name"] == "migrate")
        assert init["image"] == f"git.allegedly.works/ducktape-ci/{migration}:unset"
        assert init["imagePullPolicy"] == "IfNotPresent"
        if workload == "actions":
            assert init["command"] == ["/usr/local/bin/actions-migrate"]
    app_pod = _resource(docs, "Deployment", "app")["spec"]["template"]["spec"]
    assert not app_pod.get("initContainers")
    app_container = app_pod["containers"][0]
    assert app_container["startupProbe"]["httpGet"]["path"] == "/healthz"
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
    edge_database_env_name = env_name(McpEdgeSettings, "database_url")
    edge_database_env = {entry["name"]: entry for entry in edge_container["env"]}[edge_database_env_name]
    assert not edge_pod.get("initContainers")
    assert edge_container["startupProbe"]["failureThreshold"] == 60
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
    assert not egress_pod.get("initContainers")
    assert proxy_env["PGSSLMODE"]["value"] == "disable"
    for workload in ("history-service", "notifications"):
        pod_spec = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        for container in (*pod_spec["containers"], *pod_spec.get("initContainers", [])):
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
    assert not pod.get("initContainers")
    assert database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-session-dispatcher"
    assert container["startupProbe"]["failureThreshold"] == 60
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
    assert gateway_config["runner_security"] == {
        "mode": "mtls",
        "tls": {
            "cert_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.crt",
            "key_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.key",
            "ca_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/ca.crt",
        },
    }
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
    assert app_uri.query == "sslmode=disable"
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

    vm_template = _resource(fixture.objects, "SandboxTemplate", "runner-vm")["spec"]
    vm_spec = vm_template["vm"]["template"]["spec"]
    supervision = _resource(fixture.objects, "SandboxTemplate", SUPERVISION_TEMPLATE)["spec"]["vm"]["template"]["spec"]
    ordinary_root = next(
        volume["containerDisk"]["image"] for volume in vm_spec["volumes"] if volume["name"] == "rootdisk"
    )
    assert ordinary_root == "git.allegedly.works/ducktape-ci/agentplane-vm-guest-root:unset"
    assert supervision["volumes"] == vm_spec["volumes"]
    ordinary_boot = vm_spec["domain"]["firmware"]["kernelBoot"]
    assert supervision["domain"]["firmware"]["kernelBoot"] == {
        **ordinary_boot,
        "kernelArgs": ordinary_boot["kernelArgs"] + " " + SUPERVISION_KERNEL_ARG,
    }
    interface = next(item for item in vm_spec["domain"]["devices"]["interfaces"] if item["name"] == "default")
    assert {"port": 9090} in interface["ports"]
    config_map = _resource(fixture.objects, "ConfigMap", "runner-vm-config")
    config = json.loads(config_map["data"]["runner.json"])
    assert config["codex_readiness"] == {
        "exec_server_ready_timeout_ms": 60_000,
        "exec_server_probe_timeout_ms": 5_000,
        "exec_server_dependency_timeout_ms": 120_000,
    }
    pod_config = json.loads(_resource(fixture.objects, "ConfigMap", "runner-config")["data"]["runner.json"])
    assert "codex_readiness" not in pod_config
    assert config["control"] == {
        "listen_address": "0.0.0.0:9090",
        "security": {
            "mode": "mtls",
            "tls": {
                "cert_file": "/run/agentplane/runner-tls/tls.crt",
                "key_file": "/run/agentplane/runner-tls/tls.key",
                "ca_file": "/run/agentplane/runner-trust/ca-certificates.crt",
            },
        },
    }


def test_central_sampler_fixtures_render_app_ingress_gateway_and_history_wiring() -> None:
    for fixture, vm in ((_filled_fixture(), False), (_filled_vm_fixture(), True)):
        assert fixture.central_sampler
        assert fixture.vm_sandboxes is vm
        docs = fixture.objects
        if vm:
            assert _resource(docs, "SandboxTemplate", "runner-vm")

        app_config = yaml.safe_load(_resource(docs, "ConfigMap", "app-config")["data"]["config.yaml"])
        catalog = ModelCatalog.model_validate(app_config["models"])
        assert "central_sampler" not in app_config["models"]
        assert catalog.harnesses[Harness.CENTRAL_SAMPLER] == [fixture_chart.GPT6_LUNA_RESPONSES.id]

        deployment = _resource(docs, "Deployment", central_sampler.NAME)
        assert deployment["spec"]["replicas"] == 1
        assert deployment["spec"]["strategy"]["type"] == "Recreate"
        pod = deployment["spec"]["template"]["spec"]
        assert not pod.get("initContainers")
        assert pod["serviceAccountName"] == central_sampler.NAME
        container = next(container for container in pod["containers"] if container["name"] == central_sampler.NAME)
        assert container["startupProbe"]["grpc"]["port"] == central_sampler.PORT
        database_env = next(
            entry for entry in container["env"] if entry["name"] == env_name(CentralSamplerSettings, "database_url")
        )
        assert database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-central-sampler"
        settings = CentralSamplerSettings.model_validate_json(
            _resource(docs, "ConfigMap", f"{central_sampler.NAME}-settings")["data"]["settings.json"]
        )
        ingress_connection = settings.connections["llm-ingress"]
        assert ingress_connection.base_url == llm_ingress.proxied_url(NAMESPACE)
        assert ingress_connection.auth.type == "bearer"
        assert str(ingress_connection.auth.credential_file) == "/var/run/secrets/central-sampler/llm"
        assert set(settings.connections) == {"llm-ingress"}
        assert settings.rpc_timeout_seconds == 10
        assert settings.executor_initialize_timeout_seconds == (120 if vm else 30)
        without_initialize_timeout = CentralSamplerSettings.model_validate(
            settings.model_dump(exclude={"executor_initialize_timeout_seconds"})
        )
        assert without_initialize_timeout.executor_initialize_timeout_seconds == 30
        invalid_timeout = settings.model_dump()
        invalid_timeout["executor_initialize_timeout_seconds"] = MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS + 1
        with pytest.raises(ValidationError):
            CentralSamplerSettings.model_validate(invalid_timeout)
        assert set(settings.models) == {fixture_chart.GPT6_LUNA_RESPONSES.id}
        model = settings.models[fixture_chart.GPT6_LUNA_RESPONSES.id]
        assert model.connection == "llm-ingress"
        assert model.provider_model == fixture_chart.GPT6_LUNA_RESPONSES.id
        assert model.api == "responses"
        assert any(
            mount["name"] == "caller-tokens" and mount["mountPath"] == "/var/run/secrets/central-sampler"
            for mount in container["volumeMounts"]
        )
        token_reviewer = _resource(docs, "ClusterRole", f"{NAMESPACE}-central-sampler-token-reviewer")
        assert token_reviewer["rules"] == [
            {"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]}
        ]
        database_uri = urlsplit(_resource(docs, "Secret", "postgres-central-sampler")["stringData"]["uri"])
        assert database_uri.username == "central_sampler"
        assert database_uri.path == "/central_sampler"
        assert database_uri.hostname == f"{fixture_database.SERVICE}.{NAMESPACE}.svc"

        projected = next(
            volume["projected"]["sources"] for volume in pod["volumes"] if volume["name"] == "caller-tokens"
        )
        token_audiences = {
            source["serviceAccountToken"]["path"]: source["serviceAccountToken"]["audience"] for source in projected
        }
        assert token_audiences == {
            "llm": WORKLOAD_TOKEN_AUDIENCE,
            "gateway": runner_gateway.TOKEN_AUDIENCE,
            "history": history_service.RUNNER_INGRESS_AUDIENCE,
        }
        assert _resource(docs, "Service", central_sampler.NAME)["spec"]["ports"][0]["port"] == central_sampler.PORT

        llm_config = yaml.safe_load(_resource(docs, "ConfigMap", "llm-ingress-settings")["data"]["settings.yaml"])
        assert llm_config["central_sampler_accounts"] == [f"{NAMESPACE}/{central_sampler.NAME}"]
        gateway_config = yaml.safe_load(
            _resource(docs, "ConfigMap", "runner-gateway-settings")["data"]["settings.yaml"]
        )
        assert gateway_config["central_sampler"]["target"] == central_sampler.target(NAMESPACE)
        assert f"{NAMESPACE}/{central_sampler.NAME}" in gateway_config["executor_callers"]
        gateway_pod = _resource(docs, "Deployment", "runner-gateway")["spec"]["template"]["spec"]
        gateway_tokens = next(
            volume["projected"]["sources"]
            for volume in gateway_pod["volumes"]
            if volume["name"] == "runner-gateway-token"
        )
        assert any(
            source["serviceAccountToken"]["audience"] == central_sampler.TOKEN_AUDIENCE
            and source["serviceAccountToken"]["path"] == "central-sampler"
            for source in gateway_tokens
        )
        history_config = yaml.safe_load(_resource(docs, "ConfigMap", "history-service-config")["data"]["config.yaml"])
        assert {"namespace": NAMESPACE, "name": central_sampler.NAME} in history_config["ingress"]["sampler_accounts"]


if __name__ == "__main__":
    pytest_bazel.main()
