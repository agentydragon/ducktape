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
from more_itertools import one
from pydantic import AnyHttpUrl
from python.runfiles import Runfiles

# Each service module exports `Settings`; aliases keep their separate config contracts explicit.
from agentplane.central_sampler.settings import Settings as CentralSamplerSettings
from agentplane.egress.settings import Settings as EgressSettings
from agentplane.env.runtime.harness import Harness
from agentplane.environment_runtime_transport.config import HISTORY_SERVICE_URI, LLM_INGRESS_URI
from agentplane.identity.auth.audience import WORKLOAD_TOKEN_AUDIENCE
from agentplane.llm_ingress.settings import Settings as LlmIngressSettings
from agentplane.mcp_edge.oauth_settings import OAuthSettings
from agentplane.sandboxes.runtimeconfig.model_catalog import ModelCatalog
from agentplane.system_tests.deployment import fixture_chart, fixture_database, rendered_fixture
from agentplane.system_tests.deployment.rendered_fixture import LocalModel, McpInputs, RuntimeInputs
from agentplane.system_tests.k3s.namespaces import NAMESPACE
from agentplane.system_tests.vm import infrastructure as vm_infrastructure, sandboxes as vm_sandboxes
from agentplane.system_tests.vm.block_storage import STORAGE_CLASS
from agentplane.system_tests.vm.configuration import OOM_TEMPLATE, SUPERVISION_KERNEL_ARG, SUPERVISION_TEMPLATE
from cluster.cdk8s.agentplane import (
    central_sampler,
    environment_firecracker,
    environment_vm,
    history_service,
    llm_ingress,
    mcp_edge,
    runner_gateway,
    session_dispatcher,
)
from util.settings_contract import env_name

_TEST_LOCAL_MODEL = LocalModel(url="http://scripted-model.k3s-fixture.invalid:4000")

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


def _host_port(address: str) -> tuple[str, int]:
    parsed = urlsplit(address if "://" in address else f"//{address}")
    assert parsed.hostname is not None
    assert parsed.port is not None
    return parsed.hostname, parsed.port


def _filled_fixture(runfiles: Runfiles) -> rendered_fixture.RenderedFixture:
    cached = rendered_fixture.read(runfiles.root() / os.environ["K3S_FIXTURE_RLOCATION"])
    return rendered_fixture.fill(
        cached,
        RuntimeInputs(
            ca_cert="fixture-ca\n", ca_key="fixture-key\n", system_roots="system-roots\n", cluster_ca="cluster-ca\n"
        ),
        _TEST_LOCAL_MODEL,
        _MCP,
        None,
    )


@pytest.fixture(scope="module")
def _vm_fixture_template(runfiles: Runfiles) -> rendered_fixture.RenderedFixture:
    return rendered_fixture.read(runfiles.root() / os.environ["K3S_VM_FIXTURE_RLOCATION"])


@pytest.fixture
def vm_fixture(_vm_fixture_template: rendered_fixture.RenderedFixture) -> rendered_fixture.RenderedFixture:
    return _vm_fixture_template.model_copy(deep=True)


def test_direct_fixture_uses_gateway_and_embedded_migrations(runfiles: Runfiles) -> None:
    fixture = _filled_fixture(runfiles)
    assert fixture.central_sampler
    assert "k3s-env-placeholder-" not in fixture.model_dump_json()
    docs = fixture.objects
    # The k3s stack applies these objects directly, but installs none of these controllers/CRDs.
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
    gateway_certificate = _resource(docs, "Certificate", "agentplane-runner-gateway")
    assert gateway_certificate["spec"]["uris"] == ["spiffe://agentplane/runner-gateway"]
    assert set(gateway_certificate["spec"]["usages"]) == {"digital signature", "client auth"}
    dispatcher_certificate = _resource(docs, "Certificate", "runner-gateway-workload")
    assert dispatcher_certificate["spec"]["dnsNames"] == [f"session-dispatcher.{NAMESPACE}.svc.cluster.local"]
    workload_issuer = _resource(docs, "ClusterIssuer", "agentplane-workload")
    workload_ca = one(
        doc
        for doc in docs
        if doc["kind"] == "Certificate" and doc["spec"]["secretName"] == workload_issuer["spec"]["ca"]["secretName"]
    )
    assert workload_ca["metadata"]["namespace"] == "cert-manager"
    assert workload_ca["spec"]["issuerRef"] == {"name": "cluster-internal-ca", "kind": "ClusterIssuer"}
    assert _resource(docs, "Bundle", "agentplane-workload-ca-bundle")["spec"]["sources"] == [
        {"secret": {"name": workload_ca["spec"]["secretName"], "key": "tls.crt"}}
    ]
    trust_bundle = _resource(docs, "Bundle", "cluster-internal-ca-bundle")
    root = _resource(docs, "Certificate", "test-runner-root")
    assert trust_bundle["spec"]["sources"] == [{"secret": {"name": root["spec"]["secretName"], "key": "ca.crt"}}]
    assert trust_bundle["spec"]["target"]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": fixture.namespace
    }
    app_pod = _resource(docs, "Deployment", "app")["spec"]["template"]["spec"]
    app_container = app_pod["containers"][0]
    assert app_container["startupProbe"]["httpGet"]["path"] == "/healthz"
    actions_container = _resource(docs, "Deployment", "actions")["spec"]["template"]["spec"]["containers"][0]
    actions_settings = yaml.safe_load(_resource(docs, "ConfigMap", "actions-settings")["data"]["settings.yaml"])
    assert actions_settings["connection_delegation_actor"] == {"namespace": NAMESPACE, "name": "mcp-edge"}
    for workload in ("actions", "notifications", "history-service", "runner-gateway", "llm-ingress"):
        pod = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        container = pod["containers"][0]
        probes = (
            ("readinessProbe", "livenessProbe") if workload == "llm-ingress" else ("readinessProbe", "startupProbe")
        )
        for probe in probes:
            command = container[probe]["exec"]["command"]
            assert command[:2] == ["/healthprobe", "--target"]
            assert "--ca-file" in command
            assert "--server-uri" in command
            assert "--skip-verify" not in command
    egress_proxy = _resource(docs, "Deployment", "egress")["spec"]["template"]["spec"]["containers"][0]
    assert "127.0.0.1:8081" in egress_proxy["readinessProbe"]["exec"]["command"]
    edge_pod = _resource(docs, "Deployment", "mcp-edge")["spec"]["template"]["spec"]
    assert not edge_pod.get("initContainers")
    edge_container = edge_pod["containers"][0]
    edge_config = yaml.safe_load(_resource(docs, "ConfigMap", "mcp-edge-settings")["data"]["settings.yaml"])
    assert edge_config["actions_target"] == (
        f"{fixture_chart.ACTIONS_SERVICE.fqdn}:{fixture_chart.ACTIONS_SERVICE.port.number}"
    )
    app_container = _resource(docs, "Deployment", "app")["spec"]["template"]["spec"]["containers"][0]
    app_env = {entry["name"]: entry for entry in app_container["env"]}
    app_settings = yaml.safe_load(_resource(docs, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert AnyHttpUrl(app_settings["connection_service_url"]) == AnyHttpUrl(fixture_chart.MCP_EDGE_SERVICE.url)
    assert "AGENTPLANE_CONNECTION_SERVICE_URL" not in app_env
    assert "AGENTPLANE_ACTIONS_OAUTH" not in {entry["name"] for entry in actions_container["env"]}
    edge_database_env_name = mcp_edge.DATABASE_URL_ENV
    edge_database_env = {entry["name"]: entry for entry in edge_container["env"]}[edge_database_env_name]
    assert edge_database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-mcp-edge"
    edge_settings_env = {
        entry["name"] for entry in edge_container["env"] if entry["name"].startswith("AGENTPLANE_MCP_EDGE_")
    }
    assert edge_settings_env == {mcp_edge.Settings.config_file_env, mcp_edge.DATABASE_URL_ENV}
    egress_pod = _resource(docs, "Deployment", "egress")["spec"]["template"]["spec"]
    proxy = next(container for container in egress_pod["containers"] if container["name"] == "proxy")
    egress_config = yaml.safe_load(_resource(docs, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"])
    assert egress_config["forward"]["listen_address"] == "0.0.0.0:8888"
    assert egress_config["rules_namespace"] == NAMESPACE
    proxy_env = {entry["name"]: entry for entry in proxy["env"]}
    assert {name for name in proxy_env if name.startswith("AGENTPLANE_EGRESS_")} == {
        EgressSettings.config_file_env,
        EgressSettings.database_url_env,
    }
    assert proxy_env[EgressSettings.database_url_env]["valueFrom"]["secretKeyRef"] == {
        "name": "postgres-egress",
        "key": "uri",
    }
    proxy_certificate = _resource(docs, "Certificate", "agentplane-egress-proxy")["spec"]
    assert proxy_certificate["uris"] == ["spiffe://agentplane/egress-proxy"]
    assert proxy_certificate["dnsNames"] == [f"egress-admin.{NAMESPACE}.svc.cluster.local"]
    assert proxy_certificate["secretName"] == "agentplane-egress-proxy-tls"
    assert proxy_certificate["issuerRef"] == {"name": "agentplane-workload", "kind": "ClusterIssuer"}
    assert any(mount["name"] == "svid" for mount in proxy["volumeMounts"])
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
    assert proxy_env["PGSSLMODE"]["value"] == "disable"
    for workload in ("history-service", "notifications"):
        pod_spec = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        for container in pod_spec["containers"]:
            sslmode = {entry["name"]: entry.get("value") for entry in container.get("env", [])}.get("PGSSLMODE")
            assert sslmode == "disable", (workload, container["name"])

    gateway = _resource(docs, "Deployment", fixture_chart.RUNNER_GATEWAY)
    pod = gateway["spec"]["template"]["spec"]
    container = pod["containers"][0]
    database_env = next(
        entry for entry in container["env"] if entry["name"] == "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL"
    )
    assert database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-session-dispatcher"
    token_mount = next(mount for mount in container["volumeMounts"] if mount["name"] == "runner-gateway-token")
    token = next(volume for volume in pod["volumes"] if volume["name"] == "runner-gateway-token")["projected"][
        "sources"
    ][0]["serviceAccountToken"]
    gateway_env = {entry["name"]: entry for entry in container["env"]}
    assert "AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE" in gateway_env
    assert gateway_env["AGENTPLANE_RUNNER_GATEWAY_POD_IP"]["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"
    gateway_config = yaml.safe_load(
        _resource(docs, "ConfigMap", f"{fixture_chart.RUNNER_GATEWAY}-settings")["data"]["settings.yaml"]
    )
    assert gateway_config["runner_token_file"] == f"{token_mount['mountPath']}/{token['path']}"
    assert gateway_config["gateway_audience"] == token["audience"]
    assert gateway_config["operator_callers"] == [f"{NAMESPACE}/app"]
    assert gateway_config["environment_launchers"] == [f"{NAMESPACE}/actions"]
    assert gateway_config["rpc"]["listen_address"] == "0.0.0.0:8080"
    assert gateway_config["rpc"]["tls"] == {
        "cert_file": "/run/agentplane/workload-tls/tls.crt",
        "key_file": "/run/agentplane/workload-tls/tls.key",
    }
    bearer_auth = next(item for item in gateway_config["rpc"]["auth"] if item["mode"] == "kubernetes_bearer")
    assert set(bearer_auth["audiences"]) == {session_dispatcher.TOKEN_AUDIENCE, runner_gateway.TOKEN_AUDIENCE}
    assert {item["mode"] for item in gateway_config["rpc"]["auth"]} == {"kubernetes_bearer", "spiffe"}
    assert gateway_config["runner_security"] == {
        "mode": "mtls",
        "tls": {
            "cert_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.crt",
            "key_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.key",
            "ca_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/ca.crt",
        },
    }
    assert f"{NAMESPACE}/app" in gateway_config["wake_callers"]

    gateway_pod_labels = gateway["spec"]["template"]["metadata"]["labels"]
    gateway_listener_ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    assert gateway_listener_ports["grpc"] == 8080
    for name in (fixture_chart.RUNNER_GATEWAY, fixture_chart.SESSION_DISPATCHER):
        service = _resource(docs, "Service", name)
        assert service["spec"]["selector"]
        assert all(gateway_pod_labels[key] == value for key, value in service["spec"]["selector"].items())
        assert all(port["targetPort"] == gateway_listener_ports[port["name"]] for port in service["spec"]["ports"])

    assert "workload" not in gateway_listener_ports

    dispatcher = _resource(docs, "Service", fixture_chart.SESSION_DISPATCHER)
    dispatcher_port = next(port["port"] for port in dispatcher["spec"]["ports"] if port["name"] == "grpc")
    endpoint = f"{dispatcher['metadata']['name']}.{NAMESPACE}.svc.cluster.local:{dispatcher_port}"
    app_config = yaml.safe_load(_resource(docs, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert app_config["session_dispatcher_target"] == endpoint
    assert app_config["runner_gateway_control_target"] == "{sandbox}-control.{namespace}.svc:8080"
    app_container = _resource(docs, "Deployment", "app")["spec"]["template"]["spec"]["containers"][0]
    assert app_container.get("args", []) == []
    app_env = {entry["name"]: entry for entry in app_container["env"]}
    assert app_env["AGENTPLANE_CONFIG_FILE"]["value"]
    assert {name for name in app_env if name.startswith("AGENTPLANE_")} == {
        "AGENTPLANE_CONFIG_FILE",
        "AGENTPLANE_DATABASE_URL",
        "AGENTPLANE_OIDC_CLIENT_ID",
        "AGENTPLANE_OIDC_CLIENT_SECRET",
        "AGENTPLANE_OIDC_SESSION_SECRET",
    }
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

    # The fixture's one Pod execution environment, on its local-path storage.
    template = _resource(docs, "SandboxTemplate", fixture_chart.POD_TEMPLATE)
    assert template["spec"]["stateVolume"]["claim"]["storageClassName"] == "local-path"
    runner_sidecar = template["spec"]["pod"]["spec"]["initContainers"]
    assert len(runner_sidecar) == 1
    assert runner_sidecar[0]["name"] == "egress-sidecar"
    assert runner_sidecar[0]["restartPolicy"] == "Always"
    sidecar_settings = yaml.safe_load(
        _resource(docs, "ConfigMap", f"{fixture_chart.POD_TEMPLATE}-egress-sidecar")["data"]["settings.yaml"]
    )
    platform_routes = sidecar_settings["platform_routes"]
    routes = platform_routes["routes"]
    telemetry_route = routes["telemetry"]
    pod_listener = _host_port(platform_routes["listen_address"])
    assert pod_listener[0] == "127.0.0.1"
    assert len(routes["telemetry"]["service_prefixes"]) == 3
    runner_config = json.loads(
        _resource(docs, "ConfigMap", f"{fixture_chart.POD_TEMPLATE}-config")["data"]["environment-runtime.json"]
    )
    assert _host_port(runner_config["model_config_target"]) == pod_listener
    history = history_service.service(fixture.namespace)
    assert runner_config["ingress"]["target"] == f"{history.fqdn}:{history.pod_port}"
    assert runner_config["ingress"]["svid"] == {
        "server_uri": HISTORY_SERVICE_URI,
        "proxy_address": sidecar_settings["listen_address"],
    }
    runtime = next(
        container
        for container in template["spec"]["pod"]["spec"]["containers"]
        if container["name"] == "environment-runtime"
    )
    runtime_env = {entry["name"]: entry["value"] for entry in runtime["env"] if "value" in entry}
    assert _host_port(runtime_env["OTEL_EXPORTER_OTLP_ENDPOINT"]) == pod_listener
    assert runtime_env.get("OTEL_SDK_DISABLED") != "true"
    sidecar_env = {entry["name"]: entry["value"] for entry in runner_sidecar[0]["env"] if "value" in entry}
    assert sidecar_env["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"http://{telemetry_route['upstream']}"
    assert sidecar_env.get("OTEL_SDK_DISABLED") != "true"
    assert telemetry_route["upstream"] == "alloy.monitoring.svc.cluster.local:4317"
    rules_route = routes["egress-rules"]
    assert rules_route["transport"] == {"mode": "tls_connect", "server_uri": "spiffe://agentplane/egress-proxy"}
    assert rules_route["upstream"] in egress_config["tls_passthrough_targets"]
    assert _host_port(runtime_env["AGENTPLANE_EGRESS_GRPC_URL"]) == pod_listener
    assert "AGENTPLANE_EGRESS_GRPC_CREDENTIAL" not in runtime_env
    assert sidecar_settings["proxy"]["transport"]["mode"] == "tls"
    assert sidecar_settings["proxy"]["transport"]["auth"]["mode"] == "spiffe"
    for name in ("sandbox", "sandbox-build"):
        pod_template = _resource(docs, "PodTemplate", name)
        command_pod = pod_template["template"]["spec"]
        assert command_pod["automountServiceAccountToken"] is False
        assert not command_pod.get("initContainers")
        workload = command_pod["containers"][0]
        sidecar = one(c for c in command_pod["containers"] if c["name"] == "egress-sidecar")
        projected_volumes = {v["name"] for v in command_pod["volumes"] if "projected" in v}
        assert not projected_volumes
        settings_volume = one(v for v in command_pod["volumes"] if v["name"] == "egress-sidecar-settings")
        assert settings_volume["name"] in {mount["name"] for mount in sidecar["volumeMounts"]}
        settings = yaml.safe_load(
            _resource(docs, "ConfigMap", settings_volume["configMap"]["name"])["data"]["settings.yaml"]
        )
        transport = settings["proxy"]["transport"]
        assert "audience_token_files" not in settings
        assert transport["mode"] == "tls"
        # The Action Service mounts the box's SVID there (agentplane/action_service/sandbox/inventory.go).
        assert transport["auth"] == {
            "mode": "spiffe",
            "cert_file": "/run/agentplane/workload-svid/tls.crt",
            "key_file": "/run/agentplane/workload-svid/tls.key",
        }
        assert settings["platform_routes"]["routes"]["egress-rules"]["transport"]["mode"] == "tls_connect"
        assert not any(
            source.get("serviceAccountToken", {}).get("audience") == "agentplane-egress"
            for volume in command_pod["volumes"]
            if "projected" in volume
            for source in volume["projected"]["sources"]
        )
        assert {"http_proxy", "https_proxy"} <= {entry["name"].lower() for entry in workload["env"]}
        assert all("secret" not in v for v in command_pod["volumes"])
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

    # Terraform/provider-owned OAuth settings and the upstream secret are projected from one Secret.
    assert edge_config.get("oauth") is None
    assert edge_config["oauth_file"] == "/etc/agentplane-mcp/oauth"
    assert edge_config["actions_token_file"] == "/var/run/secrets/agentplane-mcp/actions"
    edge_settings_env_names = {entry["name"] for entry in edge_container["env"]}
    assert "AGENTPLANE_MCP_EDGE_OAUTH" not in edge_settings_env_names
    edge_secret_mount = next(
        mount for mount in edge_container["volumeMounts"] if mount["mountPath"] == "/etc/agentplane-mcp"
    )
    edge_secret_volume = next(volume for volume in edge_pod["volumes"] if volume["name"] == edge_secret_mount["name"])[
        "secret"
    ]
    assert edge_secret_volume["secretName"] == "agentplane-mcp-edge-oauth"
    assert {"key": "client-secret", "path": "client-secret"} in edge_secret_volume["items"]
    assert {"key": "oauth", "path": "oauth"} in edge_secret_volume["items"]
    edge_secret = _resource(docs, "Secret", edge_secret_volume["secretName"])["stringData"]
    assert set(edge_secret) == {"oauth", "client-secret"}
    edge_oauth = OAuthSettings.model_validate_json(edge_secret["oauth"])
    assert edge_oauth.base_url == _MCP.mcp_edge_origin
    assert str(edge_oauth.upstream_client_secret_file) == "/etc/agentplane-mcp/client-secret"
    assert edge_secret["client-secret"]
    token_mount = next(
        mount for mount in edge_container["volumeMounts"] if mount["mountPath"] == "/var/run/secrets/agentplane-mcp"
    )
    token_volume = next(volume for volume in edge_pod["volumes"] if volume["name"] == token_mount["name"])
    token = token_volume["projected"]["sources"][0]["serviceAccountToken"]
    assert token["audience"] == WORKLOAD_TOKEN_AUDIENCE
    assert edge_config["actions_token_file"] == f"{token_mount['mountPath']}/{token['path']}"
    operator_config = yaml.safe_load(operator["config.yaml"])
    clients = {client["id"]: client for client in operator_config["staticClients"]}
    assert clients[edge_oauth.upstream_client_id]["redirectURIs"] == [f"{_MCP.mcp_edge_origin}/auth/callback"]
    assert clients["test-mcp-linkage"]["redirectURIs"] == [f"{_MCP.app_origin}/mcp-linkage/callback"]
    assert edge_config["policy_namespace"] == fixture.namespace
    assert edge_config["actions_token_file"] == "/var/run/secrets/agentplane-mcp/actions"
    caller = _resource(docs, "Identity", "mcp-edge-test-caller")
    assert caller["metadata"]["namespace"] == fixture.namespace
    binding = _resource(docs, "PolicyBinding", "mcp-edge-test-caller")
    assert binding["spec"] == {
        "subject": {"identity": {"name": "mcp-edge-test-caller"}},
        "policySets": ["mcp-edge-test-caller-session-delegation"],
    }
    policy = _resource(docs, "PolicySet", "mcp-edge-test-caller-session-delegation")
    assert policy["spec"]["rules"] == [
        {
            "type": "action_auto_approve",
            "policy": {"type": "exact_actions", "actions": {"environments": ["list", "get"], "sessions": ["create"]}},
        }
    ]

    assert _resource(docs, "Role", "mcp-edge")["rules"] == [
        {"apiGroups": ["agentplane.allegedly.works"], "resources": ["identities"], "verbs": ["get", "list", "watch"]}
    ]


def test_vm_native_profile_uses_single_manual_root_and_oom_only_sampling(
    vm_fixture: rendered_fixture.RenderedFixture,
) -> None:
    control = vm_fixture.model_copy(deep=True)
    selected = _resource(vm_fixture.objects, "SandboxTemplate", OOM_TEMPLATE)
    other = _resource(vm_fixture.objects, "SandboxTemplate", "environment-vm")
    original_args = selected["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    original_other_args = other["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    vm_sandboxes.select_native_profile(vm_fixture.objects, enabled=True, profile_root=True)
    selected_args = selected["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    other_args = other["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    assert other_args == original_other_args
    assert selected_args == original_args + " agentplane.native-profile=1"
    assert "agentplane.native-profile=1" not in other_args
    selected_volumes = selected["spec"]["vm"]["template"]["spec"]["volumes"]
    other_volumes = other["spec"]["vm"]["template"]["spec"]["volumes"]
    assert next(volume for volume in selected_volumes if volume["name"] == "rootdisk")["containerDisk"][
        "image"
    ].endswith("/agentplane-vm-guest-root-profile:unset")
    assert next(volume for volume in other_volumes if volume["name"] == "rootdisk")["containerDisk"]["image"].endswith(
        "/agentplane-vm-guest-root-profile:unset"
    )
    vm_sandboxes.select_native_profile(control.objects, enabled=False, profile_root=True)
    control_oom = _resource(control.objects, "SandboxTemplate", OOM_TEMPLATE)
    control_spec = control_oom["spec"]["vm"]["template"]["spec"]
    assert "agentplane.native-profile=1" not in control_spec["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    assert next(volume for volume in control_spec["volumes"] if volume["name"] == "rootdisk")["containerDisk"][
        "image"
    ].endswith("/agentplane-vm-guest-root-profile:unset")


def test_kubevirt_baseline_selects_emulation_from_the_explicit_kvm_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTPLANE_K3S_REQUIRE_KVM", raising=False)
    tcg = vm_infrastructure.baseline_objects()[0]["spec"]["configuration"]["developerConfiguration"]
    assert tcg["useEmulation"] is True

    monkeypatch.setenv("AGENTPLANE_K3S_REQUIRE_KVM", "1")
    kvm = vm_infrastructure.baseline_objects()[0]["spec"]["configuration"]["developerConfiguration"]
    assert kvm["useEmulation"] is False


def test_vm_boot_evidence_selects_only_oom_guest_at_runtime(vm_fixture: rendered_fixture.RenderedFixture) -> None:
    selected = _resource(vm_fixture.objects, "SandboxTemplate", OOM_TEMPLATE)
    other = _resource(vm_fixture.objects, "SandboxTemplate", "environment-vm")
    original_args = selected["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    original_other_args = other["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    vm_sandboxes.select_boot_evidence(vm_fixture.objects, enabled=True)
    selected_args = selected["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    other_args = other["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    assert other_args == original_other_args
    assert selected_args == original_args + " agentplane.boot-evidence=1"
    assert "agentplane.boot-evidence=1" not in other_args


def test_libvirt_monitor_debug_is_opt_in_and_confined_to_test_vms(vm_fixture: rendered_fixture.RenderedFixture) -> None:
    objects = vm_fixture.objects
    original = json.loads(json.dumps(objects))
    vm_sandboxes.select_libvirt_monitor_debug(objects, enabled=False)
    assert objects == original

    vm_sandboxes.select_libvirt_monitor_debug(objects, enabled=True)
    for before, after in zip(original, objects, strict=True):
        if before["kind"] != "SandboxTemplate" or "vm" not in before["spec"]:
            assert after == before
            continue
        before_meta = before["spec"]["vm"]["template"].get("metadata", {})
        after_meta = after["spec"]["vm"]["template"]["metadata"]
        assert after_meta["labels"] == {**before_meta.get("labels", {}), "debugLogs": "true"}
        assert after_meta["annotations"] == {
            **before_meta.get("annotations", {}),
            "kubevirt.io/libvirt-log-filters": "1:qemu.qemu_monitor 3:*",
        }


def test_vm_fixture_uses_raw_block_workspace_and_filesystem_state(vm_fixture: rendered_fixture.RenderedFixture) -> None:
    template = _resource(vm_fixture.objects, "SandboxTemplate", "environment-vm")["spec"]
    state = template["stateVolume"]
    workspace = template["workspaceVolume"]

    assert state["claim"]["storageClassName"] == "local-path"
    assert state["claim"].get("volumeMode", "Filesystem") == "Filesystem"
    assert workspace["layout"] == {"type": "BlockFilesystem", "filesystem": "ext4"}
    assert workspace["claim"]["storageClassName"] == STORAGE_CLASS
    assert workspace["claim"]["volumeMode"] == "Block"

    vm_template = _resource(vm_fixture.objects, "SandboxTemplate", "environment-vm")["spec"]
    vm_spec = vm_template["vm"]["template"]["spec"]
    runner_disk = next(disk for disk in vm_spec["domain"]["devices"]["disks"] if disk["name"] == "runner")
    assert runner_disk["serial"] == "agentplane-runner"  # vm.nix mounts virtio-agentplane-runner from fstab.
    supervision = _resource(vm_fixture.objects, "SandboxTemplate", SUPERVISION_TEMPLATE)["spec"]["vm"]["template"][
        "spec"
    ]
    oom_template = _resource(vm_fixture.objects, "SandboxTemplate", OOM_TEMPLATE)["spec"]["vm"]["template"]["spec"]
    assert supervision["volumes"] == vm_spec["volumes"]
    ordinary_config_volume = next(
        volume for volume in vm_spec["volumes"] if volume.get("configMap", {}).get("name") == "environment-vm-config"
    )
    oom_config_volume = next(
        volume for volume in oom_template["volumes"] if volume["name"] == ordinary_config_volume["name"]
    )
    assert oom_config_volume["configMap"]["name"] != ordinary_config_volume["configMap"]["name"]
    assert oom_template["domain"]["resources"] == vm_spec["domain"]["resources"]
    ordinary_boot = vm_spec["domain"]["firmware"]["kernelBoot"]
    assert supervision["domain"]["firmware"]["kernelBoot"] == {
        **ordinary_boot,
        "kernelArgs": ordinary_boot["kernelArgs"] + " " + SUPERVISION_KERNEL_ARG,
    }
    interface = next(item for item in vm_spec["domain"]["devices"]["interfaces"] if item["name"] == "default")
    config_map = _resource(vm_fixture.objects, "ConfigMap", "environment-vm-config")
    config = json.loads(config_map["data"]["environment-runtime.json"])
    relay = yaml.safe_load(
        _resource(vm_fixture.objects, "ConfigMap", "launcher-relay-egress-sidecar")["data"]["settings.yaml"]
    )
    vm_listener = _host_port(relay["platform_routes"]["listen_address"])
    assert vm_listener[0] == "0.0.0.0"
    guest_listener = ("10.0.2.1", vm_listener[1])
    assert _host_port(config["model_config_target"]) == guest_listener
    history = history_service.service(vm_fixture.namespace)
    assert config["ingress"]["target"] == f"{history.fqdn}:{history.pod_port}"
    assert config["ingress"]["svid"]["server_uri"] == HISTORY_SERVICE_URI
    assert _host_port(config["ingress"]["svid"]["proxy_address"]) == (
        "10.0.2.1",
        _host_port(relay["listen_address"])[1],
    )
    assert _host_port(config["harness_environment"]["values"]["AGENTPLANE_EGRESS_GRPC_URL"]) == guest_listener
    assert _host_port(config["harness_environment"]["values"]["AGENTPLANE_NOTIFICATIONS_GRPC_URL"]) == guest_listener
    vm_runtime_env = dict(line.split("=", 1) for line in config_map["data"]["environment-runtime.env"].splitlines())
    assert _host_port(vm_runtime_env["OTEL_EXPORTER_OTLP_ENDPOINT"].strip('"')) == guest_listener
    assert "AGENTPLANE_NOTIFICATIONS_GRPC_CREDENTIAL" not in config["harness_environment"]["values"]
    assert "AGENTPLANE_EGRESS_GRPC_CREDENTIAL" not in config["harness_environment"]["values"]
    oom_config_map = _resource(vm_fixture.objects, "ConfigMap", oom_config_volume["configMap"]["name"])
    oom_config = json.loads(oom_config_map["data"]["environment-runtime.json"])
    oom_resources = oom_config["environment"]["resources"]
    ordinary_resources = config["environment"]["resources"]
    assert oom_resources == ordinary_resources
    control = config["control"]
    assert {"port": int(control["listen_address"].rsplit(":", 1)[1])} in interface["ports"]
    assert control["security"] == {
        "mode": "mtls",
        "tls": {
            "cert_file": "/run/agentplane/environment-runtime-tls/tls.crt",
            "key_file": "/run/agentplane/environment-runtime-tls/tls.key",
            "ca_file": "/run/agentplane/environment-runtime-trust/ca-certificates.crt",
            "client_ca_file": "/run/agentplane/environment-runtime-workload-trust/ca-certificates.crt",
        },
    }


def test_pod_central_sampler_fixture_renders_app_ingress_gateway_and_history_wiring(runfiles: Runfiles) -> None:
    fixture = _filled_fixture(runfiles)
    assert fixture.central_sampler
    assert not fixture.vm_sandboxes
    docs = fixture.objects

    app_config = yaml.safe_load(_resource(docs, "ConfigMap", "app-config")["data"]["config.yaml"])
    catalog = ModelCatalog.model_validate(app_config["models"])
    assert "central_sampler" not in app_config["models"]
    assert catalog.harnesses[Harness.CENTRAL_SAMPLER] == [fixture_chart.GPT6_LUNA_RESPONSES.id]

    deployment = _resource(docs, "Deployment", central_sampler.NAME)
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == central_sampler.NAME
    container = next(container for container in pod["containers"] if container["name"] == central_sampler.NAME)
    service = _resource(docs, "Service", central_sampler.NAME)
    assert service["spec"]["selector"]
    assert all(
        deployment["spec"]["template"]["metadata"]["labels"][key] == value
        for key, value in service["spec"]["selector"].items()
    )
    assert service["spec"]["ports"][0]["targetPort"] == container["ports"][0]["containerPort"]
    assert container["startupProbe"]["grpc"]["port"] == container["ports"][0]["containerPort"]
    database_env = next(
        entry for entry in container["env"] if entry["name"] == env_name(CentralSamplerSettings, "database_url")
    )
    assert database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-central-sampler"
    settings = CentralSamplerSettings.model_validate_json(
        _resource(docs, "ConfigMap", f"{central_sampler.NAME}-settings")["data"]["settings.json"]
    )
    ingress_connection = settings.connections["llm-ingress"]
    assert ingress_connection.base_url == (
        f"https://{llm_ingress.service(NAMESPACE).fqdn}:{llm_ingress.service(NAMESPACE).port.number}"
    )
    assert ingress_connection.auth.type == "bearer"
    assert str(ingress_connection.auth.credential_file) == "/var/run/secrets/central-sampler/llm"
    assert ingress_connection.tls is not None
    assert str(ingress_connection.tls.ca_file) == "/run/agentplane/workload-trust/ca-certificates.crt"
    assert ingress_connection.tls.server_uri == LLM_INGRESS_URI
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

    projected = next(volume["projected"]["sources"] for volume in pod["volumes"] if volume["name"] == "caller-tokens")
    token_audiences = {
        source["serviceAccountToken"]["path"]: source["serviceAccountToken"]["audience"] for source in projected
    }
    assert token_audiences == {
        "llm": WORKLOAD_TOKEN_AUDIENCE,
        "gateway": runner_gateway.TOKEN_AUDIENCE,
        "history": history_service.RUNNER_INGRESS_AUDIENCE,
    }

    llm_pod = _resource(docs, "Deployment", "llm-ingress")["spec"]["template"]["spec"]
    llm_container = llm_pod["containers"][0]
    llm_env = {entry["name"]: entry for entry in llm_container["env"]}
    assert {name for name in llm_env if name.startswith("AGENTPLANE_LLM_INGRESS_")} == {
        LlmIngressSettings.config_file_env,
        LlmIngressSettings.litellm_key_env,
    }
    assert llm_env[LlmIngressSettings.litellm_key_env]["valueFrom"]["secretKeyRef"]["key"] == "api-key"
    llm_config = yaml.safe_load(_resource(docs, "ConfigMap", "llm-ingress-settings")["data"]["settings.yaml"])
    assert llm_config["allowed_service_account_namespaces"] == [NAMESPACE]
    assert llm_config["policy_namespace"] == NAMESPACE
    assert llm_config["central_sampler_identity_callers"] is False
    assert llm_config["token_audience"] == WORKLOAD_TOKEN_AUDIENCE
    assert AnyHttpUrl(llm_config["litellm_url"]) == AnyHttpUrl(_TEST_LOCAL_MODEL.url)
    assert llm_config["log_levels"] == {}
    assert "litellm_key" not in llm_config
    assert llm_config["central_sampler_accounts"] == [f"{NAMESPACE}/{central_sampler.NAME}"]
    gateway_config = yaml.safe_load(_resource(docs, "ConfigMap", "runner-gateway-settings")["data"]["settings.yaml"])
    assert gateway_config["central_sampler"]["target"] == central_sampler.target(NAMESPACE)
    assert f"{NAMESPACE}/{central_sampler.NAME}" in gateway_config["executor_callers"]
    gateway_pod = _resource(docs, "Deployment", "runner-gateway")["spec"]["template"]["spec"]
    gateway_tokens = next(
        volume["projected"]["sources"] for volume in gateway_pod["volumes"] if volume["name"] == "runner-gateway-token"
    )
    assert any(
        source["serviceAccountToken"]["audience"] == central_sampler.TOKEN_AUDIENCE
        and source["serviceAccountToken"]["path"] == "central-sampler"
        for source in gateway_tokens
    )
    history_config = yaml.safe_load(_resource(docs, "ConfigMap", "history-service-config")["data"]["config.yaml"])
    assert history_config["migration_timeout_s"] == 600
    assert history_config["fold_concurrency"] == 4
    assert history_config["native_frame_read_batch_size"] == 200
    assert "database_url" not in history_config
    assert "log_levels" not in history_config
    assert {"namespace": NAMESPACE, "name": central_sampler.NAME} in history_config["ingress"]["sampler_accounts"]
    history_pod = _resource(docs, "Deployment", "history-service")["spec"]["template"]["spec"]
    history_container = history_pod["containers"][0]
    history_env = {entry["name"]: entry for entry in history_container["env"]}
    assert history_env["AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"]["value"] == (
        "/etc/agentplane-history-service/config.yaml"
    )
    history_database_env = history_env["AGENTPLANE_HISTORY_SERVICE_DATABASE_URL"]
    assert history_database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-sandbox-service"


def test_firecracker_fixture_uses_production_construct_and_discloses_cilium_boundary(runfiles: Runfiles) -> None:
    fixture = rendered_fixture.read(runfiles.root() / os.environ["K3S_FIRECRACKER_FIXTURE_RLOCATION"])
    assert fixture.firecracker_sandboxes
    assert not fixture.vm_sandboxes
    assert not any(doc["kind"] == "CiliumNetworkPolicy" for doc in fixture.objects)

    template = _resource(fixture.objects, "SandboxTemplate", environment_firecracker.NAME)["spec"]
    assert "vm" not in template
    firecracker = template["firecracker"]
    assert firecracker["bootImage"] == environment_firecracker.BOOT_IMAGE
    assert firecracker["runnerImage"] == environment_vm.RUNNER_DISK_IMAGE
    assert firecracker["vcpuCount"] > 0
    config = json.loads(
        _resource(fixture.objects, "ConfigMap", "environment-firecracker-config")["data"]["environment-runtime.json"]
    )
    relay = yaml.safe_load(
        _resource(fixture.objects, "ConfigMap", "environment-firecracker-relay")["data"]["settings.yaml"]
    )
    listener = _host_port(relay["platform_routes"]["listen_address"])
    guest_listener = (environment_firecracker.GATEWAY, listener[1])
    assert _host_port(config["harness_environment"]["values"]["AGENTPLANE_EGRESS_GRPC_URL"]) == guest_listener
    assert _host_port(config["model_config_target"]) == guest_listener
    assert config["control"]["security"] == {
        "mode": "mtls",
        "tls": {"workload_api_socket": "unix:///run/agentplane/environment-runtime-tls/workload.sock"},
    }

    for volume in (template["stateVolume"], template["workspaceVolume"]):
        assert volume["layout"] == {"type": "RawDiskImage", "filesystem": "ext4"}
        assert volume["claim"]["storageClassName"] == "local-path"
        assert volume["claim"]["volumeMode"] == "Filesystem"
    assert template["stateVolume"]["claim"]["resources"]["requests"]["storage"] == "100Gi"

    pod = firecracker["pod"]["spec"]
    launcher = next(container for container in pod["containers"] if container["name"] == "environment-runtime")
    assert launcher["image"] == environment_firecracker.LAUNCHER_IMAGE
    assert launcher["resources"]["requests"]["devices.kubevirt.io/kvm"] == 1
    assert launcher["resources"]["limits"]["devices.kubevirt.io/kvm"] == 1
    assert any(mount["name"] == "tun" and mount["mountPath"] == "/dev/net/tun" for mount in launcher["volumeMounts"])
    assert next(volume for volume in pod["volumes"] if volume["name"] == "tun")["hostPath"] == {
        "path": "/dev/net/tun",
        "type": "CharDevice",
    }


if __name__ == "__main__":
    pytest_bazel.main()
