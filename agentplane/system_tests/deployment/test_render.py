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

from agentplane.egress.settings import Settings as EgressSettings
from agentplane.env.runtime.harness import Harness
from agentplane.llm_ingress.settings import Settings as LlmIngressSettings
from agentplane.mcp_edge.oauth_settings import OAuthSettings
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    CENTRAL_SAMPLER_URI,
    GATEWAY_SESSIONS_URI,
    HISTORY_SERVICE_URI,
    LLM_INGRESS_URI,
    MCP_EDGE_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
)

# Each service module exports `Settings`; aliases keep their separate config contracts explicit.
from agentplane.sampler.settings import Settings as CentralSamplerSettings
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
    environment_service,
    environment_vm,
    history_service,
    llm_ingress,
    mcp_edge,
    runner_gateway,
    workload_tls,
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


def test_fixture_grants_no_tokenreviews(mcp_fixture: rendered_fixture.RenderedFixture) -> None:
    docs = mcp_fixture.objects
    for doc in docs:
        if doc["kind"] in {"Role", "ClusterRole"}:
            for rule in doc.get("rules", []):
                assert "tokenreviews" not in rule.get("resources", []), doc["metadata"]["name"]
        if doc["kind"] in {"RoleBinding", "ClusterRoleBinding"}:
            assert "token-reviewer" not in doc["roleRef"]["name"], doc["metadata"]["name"]


def test_fixture_exposes_environments(mcp_fixture: rendered_fixture.RenderedFixture) -> None:
    docs = mcp_fixture.objects
    deployment = _resource(docs, "Deployment", "environments")
    assert deployment["spec"]["replicas"] == 1
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert _resource(docs, "Service", "environments")["spec"]["selector"].items() <= labels.items()
    certificate = _resource(docs, "Certificate", "environments-client-workload")["spec"]
    assert certificate["dnsNames"] == [f"environments.{NAMESPACE}.svc.cluster.local"]
    assert certificate["uris"] == [SANDBOX_CONTROLLER_URI]


def _host_port(address: str) -> tuple[str, int]:
    parsed = urlsplit(address if "://" in address else f"//{address}")
    assert parsed.hostname is not None
    assert parsed.port is not None
    return parsed.hostname, parsed.port


def _read_fixture(runfiles: Runfiles, location: str) -> rendered_fixture.RenderedFixture:
    return rendered_fixture.read(runfiles.root() / os.environ[location])


@pytest.fixture(scope="module")
def mcp_fixture(runfiles: Runfiles) -> rendered_fixture.RenderedFixture:
    return _read_fixture(runfiles, "K3S_FIXTURE_RLOCATION")


def _filled_fixture(cached: rendered_fixture.RenderedFixture) -> rendered_fixture.RenderedFixture:
    return rendered_fixture.fill(
        cached,
        RuntimeInputs(
            ca_cert="fixture-ca\n", ca_key="fixture-key\n", system_roots="system-roots\n", cluster_ca="cluster-ca\n"
        ),
        _TEST_LOCAL_MODEL,
        _MCP if cached.mcp_fixtures is not None else None,
        "test-operator-public-key" if cached.operator_tokens is not None else None,
    )


@pytest.fixture(scope="module")
def _vm_fixture_template(runfiles: Runfiles) -> rendered_fixture.RenderedFixture:
    return _read_fixture(runfiles, "K3S_VM_FIXTURE_RLOCATION")


@pytest.fixture
def vm_fixture(_vm_fixture_template: rendered_fixture.RenderedFixture) -> rendered_fixture.RenderedFixture:
    return _vm_fixture_template.model_copy(deep=True)


def test_pod_profile_omits_mcp_edge_database_and_keeps_app_and_action_flows(runfiles: Runfiles) -> None:
    ordinary = _read_fixture(runfiles, "K3S_DEFAULT_FIXTURE_RLOCATION")
    pod = _read_fixture(runfiles, "K3S_POD_FIXTURE_RLOCATION")
    assert pod.backend is ordinary.backend is rendered_fixture.Backend.LOCAL_MODEL
    assert pod.runtime is ordinary.runtime is rendered_fixture.SandboxRuntime.POD
    ordinary_resources = {(doc["kind"], doc["metadata"]["name"]) for doc in ordinary.objects}
    pod_resources = {(doc["kind"], doc["metadata"]["name"]) for doc in pod.objects}
    removed = ordinary_resources - pod_resources
    assert ("Deployment", "mcp-edge") in removed
    assert ("ConfigMap", "mcp-edge-settings") in removed
    assert ("ServiceAccount", "mcp-edge") in removed
    assert ("Service", "mcp-edge") in removed
    assert ("Role", "mcp-edge") in removed
    assert ("RoleBinding", "mcp-edge") in removed
    assert ("Secret", "postgres-mcp-edge") in removed
    assert all(name.startswith("mcp-edge") or name == "postgres-mcp-edge" for _, name in removed)
    _resource(pod.objects, "Deployment", "app")
    _resource(pod.objects, "Deployment", "actions")
    _resource(pod.objects, "Deployment", "notifications")
    _resource(pod.objects, "Deployment", "postgres")
    bootstrap = _resource(pod.objects, "ConfigMap", "postgres-bootstrap")["data"]["bootstrap.sql"]
    assert "CREATE ROLE app " in bootstrap
    assert "CREATE ROLE mcp_edge " not in bootstrap
    assert not any(
        "agentplane-mcp-edge:" in container["image"]
        for doc in pod.objects
        if doc["kind"] == "Deployment"
        for container in doc["spec"]["template"]["spec"]["containers"]
    )
    # The app keeps its unused Connection routes; external Connections are out of scope here.
    app_config = yaml.safe_load(_resource(pod.objects, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert AnyHttpUrl(app_config["connection_service_url"]) == AnyHttpUrl(
        f"https://{fixture_chart.MCP_EDGE_SERVICE.authority}"
    )


@pytest.mark.parametrize("location", ["K3S_DEFAULT_FIXTURE_RLOCATION", "K3S_FIXTURE_RLOCATION"])
def test_default_and_mcp_profiles_keep_real_connection_service(runfiles: Runfiles, location: str) -> None:
    fixture = _read_fixture(runfiles, location)
    edge = _resource(fixture.objects, "Deployment", "mcp-edge")
    assert any(
        "agentplane-mcp-edge:" in container["image"] for container in edge["spec"]["template"]["spec"]["containers"]
    )
    _resource(fixture.objects, "ConfigMap", "mcp-edge-settings")
    _resource(fixture.objects, "Role", "mcp-edge")
    _resource(fixture.objects, "RoleBinding", "mcp-edge")


@pytest.mark.parametrize("location", ["K3S_VM_FIXTURE_RLOCATION", "K3S_FIRECRACKER_FIXTURE_RLOCATION"])
def test_virtualized_profiles_omit_unused_mcp_edge_and_database(runfiles: Runfiles, location: str) -> None:
    fixture = _read_fixture(runfiles, location)
    assert not any(doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mcp-edge" for doc in fixture.objects)
    assert not any(
        doc["kind"] == "Secret" and doc["metadata"]["name"] == "postgres-mcp-edge" for doc in fixture.objects
    )
    _resource(fixture.objects, "Deployment", "actions")


def test_disposable_postgres_bootstraps_the_database_in_each_service_uri(
    mcp_fixture: rendered_fixture.RenderedFixture,
) -> None:
    docs = mcp_fixture.objects
    assert not any(doc["kind"] in {"Cluster", "Database"} for doc in docs)
    bootstrap = _resource(docs, "ConfigMap", "postgres-bootstrap")["data"]["bootstrap.sql"]
    # History migration 18 grants to this non-login replication role.
    assert "CREATE ROLE electric;" in bootstrap

    postgres = _resource(docs, "Deployment", fixture_database.NAME)["spec"]["template"]["spec"]
    container = one(container for container in postgres["containers"] if container["name"] == "postgres")
    secrets = {entry["name"]: entry["valueFrom"]["secretKeyRef"] for entry in container["env"] if "valueFrom" in entry}
    admin = secrets["POSTGRES_PASSWORD"]
    assert admin["key"] in _resource(docs, "Secret", admin["name"])["stringData"]

    service = _resource(docs, "Service", fixture_database.SERVICE)
    [port] = service["spec"]["ports"]
    host = f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc"
    uri_refs = {
        (ref["name"], ref["key"])
        for doc in docs
        if doc["kind"] == "Deployment"
        for client in doc["spec"]["template"]["spec"]["containers"]
        for env in client.get("env", [])
        if (ref := env.get("valueFrom", {}).get("secretKeyRef")) and ref["key"] in {"uri", "async-uri"}
    }
    assert ("postgres-app", "async-uri") in uri_refs
    for secret_name, key in uri_refs:
        credentials = _resource(docs, "Secret", secret_name)["stringData"]
        uri = urlsplit(credentials[key])
        assert uri.scheme == ("postgresql+asyncpg" if key == "async-uri" else "postgresql")
        assert (uri.hostname, uri.port) == (host, port["port"])
        assert uri.username is not None
        assert uri.path == f"/{uri.username}"
        env_name = fixture_database.password_env(uri.username)
        assert secrets[env_name]["name"] == secret_name
        assert uri.password == credentials[secrets[env_name]["key"]]
        assert f"\\getenv {uri.username}_password {env_name}" in bootstrap
        assert uri.query == "sslmode=disable"


def test_direct_fixture_uses_sessions_and_embedded_migrations(mcp_fixture: rendered_fixture.RenderedFixture) -> None:
    fixture = _filled_fixture(mcp_fixture)
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
    dispatcher_certificate = _resource(docs, "Certificate", "sessions-workload")
    assert dispatcher_certificate["spec"]["dnsNames"] == [f"sessions.{NAMESPACE}.svc.cluster.local"]
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
    actions_settings = yaml.safe_load(_resource(docs, "ConfigMap", "actions-settings")["data"]["settings.yaml"])
    assert actions_settings["connection_delegation_service"] == MCP_EDGE_URI
    assert [auth["mode"] for auth in actions_settings["rpc"]["auth"]] == ["spiffe"]
    for workload in ("actions", "notifications", "history-service", "sessions", "llm-ingress"):
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
    edge_service_ports = {port["port"] for port in _resource(docs, "Service", "mcp-edge")["spec"]["ports"]}
    assert 8080 not in edge_service_ports
    assert {8081, 8443} <= edge_service_ports
    edge_container_ports = {port["containerPort"] for port in edge_container["ports"]}
    assert edge_container_ports == {8081, 8443}
    assert edge_container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert edge_container["readinessProbe"]["httpGet"]["port"] == 8081
    assert edge_container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert edge_container["startupProbe"]["httpGet"]["path"] == "/healthz"
    edge_config = yaml.safe_load(_resource(docs, "ConfigMap", "mcp-edge-settings")["data"]["settings.yaml"])
    assert edge_config["http_port"] == 8081
    assert edge_config["actions_target"] == (fixture_chart.ACTIONS_SERVICE.authority)
    assert edge_config["actions_tls"] == {
        "ca_file": "/run/agentplane/workload-trust/ca-certificates.crt",
        "server_uri": ACTIONS_URI,
        "cert_file": "/run/agentplane/workload-tls/tls.crt",
        "key_file": "/run/agentplane/workload-tls/tls.key",
        "client_uri": MCP_EDGE_URI,
    }
    assert edge_config.get("actions_token_file") is None
    app_settings = yaml.safe_load(_resource(docs, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert AnyHttpUrl(app_settings["connection_service_url"]) == AnyHttpUrl(
        f"https://{fixture_chart.MCP_EDGE_SERVICE.authority}"
    )
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
    assert egress_config["admin_grpc_service_callers"] == ["spiffe://agentplane/app"]
    proxy_env = {entry["name"]: entry for entry in proxy["env"]}
    assert proxy_env["PGSSLMODE"]["value"] == "disable"
    for workload in ("history-service", "notifications"):
        pod_spec = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        for container in pod_spec["containers"]:
            sslmode = {entry["name"]: entry.get("value") for entry in container.get("env", [])}.get("PGSSLMODE")
            assert sslmode == "disable", (workload, container["name"])

    sessions = _resource(docs, "Deployment", "sessions")
    pod = sessions["spec"]["template"]["spec"]
    container = pod["containers"][0]
    database_env = next(
        entry for entry in container["env"] if entry["name"] == "AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL"
    )
    assert database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-session-dispatcher"
    sessions_env = {entry["name"]: entry for entry in container["env"]}
    assert "AGENTPLANE_SESSIONS_CONFIG_FILE" in sessions_env
    sessions_config = yaml.safe_load(_resource(docs, "ConfigMap", "sessions-settings")["data"]["settings.yaml"])
    assert "runner_token_file" not in sessions_config
    sessions_tls_mount = one(
        mount for mount in container["volumeMounts"] if mount["name"] == "sessions-client-workload-tls"
    )
    sessions_tls_path = PurePosixPath(sessions_tls_mount["mountPath"])
    assert sessions_config["gateway_tls"] == {
        "ca_file": workload_tls.TRUST_BUNDLE_FILE,
        "server_uri": SANDBOX_CONTROLLER_URI,
        "cert_file": str(sessions_tls_path / "tls.crt"),
        "key_file": str(sessions_tls_path / "tls.key"),
        "client_uri": GATEWAY_SESSIONS_URI,
    }
    assert not any(key.startswith("environment_") for key in sessions_config)
    assert sessions_config["rpc"]["listen_address"] == "0.0.0.0:8080"
    assert sessions_config["rpc"]["tls"] == {
        "cert_file": "/run/agentplane/workload-tls/tls.crt",
        "key_file": "/run/agentplane/workload-tls/tls.key",
    }
    assert sessions_config["rpc"]["auth"] == [{"mode": "spiffe", "ca_file": workload_tls.TRUST_BUNDLE_FILE}]
    assert "runner_security" not in sessions_config
    app_certificate = _resource(docs, "Certificate", "app-workload")["spec"]
    assert set(app_certificate["uris"]) <= set(sessions_config["wake_service_callers"])

    sessions_pod_labels = sessions["spec"]["template"]["metadata"]["labels"]
    sessions_listener_ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    assert sessions_listener_ports["grpc"] == 8080
    dispatcher = _resource(docs, "Service", "sessions")
    assert dispatcher["spec"]["selector"]
    assert dispatcher["spec"]["selector"].items() <= sessions_pod_labels.items()
    assert all(port["targetPort"] == sessions_listener_ports[port["name"]] for port in dispatcher["spec"]["ports"])
    assert "workload" not in sessions_listener_ports

    dispatcher_port = next(port["port"] for port in dispatcher["spec"]["ports"] if port["name"] == "grpc")
    endpoint = f"{dispatcher['metadata']['name']}.{NAMESPACE}.svc.cluster.local:{dispatcher_port}"
    app_config = yaml.safe_load(_resource(docs, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert app_config["session_dispatcher_target"] == endpoint
    assert app_config["session_dispatcher_tls"]["server_uri"] == SESSION_DISPATCHER_URI
    assert app_config["runner_gateway_tls"]["server_uri"] == SANDBOX_CONTROLLER_URI
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
    notifications = yaml.safe_load(_resource(docs, "ConfigMap", "notifications-settings")["data"]["settings.yaml"])
    assert notifications["session_dispatcher"]["target"] == endpoint
    assert notifications["session_dispatcher"]["tls"]["server_uri"] == SESSION_DISPATCHER_URI
    actions_config = yaml.safe_load(_resource(docs, "ConfigMap", "actions-settings")["data"]["settings.yaml"])
    assert actions_config["session_dispatcher"]["target"] == endpoint
    assert actions_config["session_dispatcher"]["tls"]["server_uri"] == SESSION_DISPATCHER_URI
    environments_service = _resource(docs, "Service", environment_service.environments_service(NAMESPACE).name)
    environments_port = one(port["port"] for port in environments_service["spec"]["ports"] if port["name"] == "grpc")
    assert actions_config["environment_service"]["target"] == (
        f"{environments_service['metadata']['name']}.{NAMESPACE}.svc.cluster.local:{environments_port}"
    )
    assert actions_config["environment_service"]["tls"]["server_uri"] == SANDBOX_CONTROLLER_URI
    role = _resource(docs, "Role", "sessions")
    assert any("sandboxes" in rule["resources"] and "patch" in rule["verbs"] for rule in role["rules"])
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
    assert runner_config["ingress"]["target"] == history.authority
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
    operator_config = yaml.safe_load(operator["config.yaml"])
    clients = {client["id"]: client for client in operator_config["staticClients"]}
    assert clients[edge_oauth.upstream_client_id]["redirectURIs"] == [f"{_MCP.mcp_edge_origin}/auth/callback"]
    assert clients["test-mcp-linkage"]["redirectURIs"] == [f"{_MCP.app_origin}/mcp-linkage/callback"]
    assert edge_config["policy_namespace"] == fixture.namespace
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
    assert config["ingress"]["target"] == history.authority
    assert config["ingress"]["svid"]["server_uri"] == HISTORY_SERVICE_URI
    assert _host_port(config["ingress"]["svid"]["proxy_address"]) == (
        "10.0.2.1",
        _host_port(relay["listen_address"])[1],
    )
    harness_env = config["harness_environment"]["values"]
    assert _host_port(harness_env["AGENTPLANE_EGRESS_GRPC_URL"]) == guest_listener
    assert _host_port(harness_env["AGENTPLANE_NOTIFICATIONS_GRPC_URL"]) == guest_listener
    assert _host_port(harness_env["AGENTPLANE_ENVIRONMENTS_URL"]) == guest_listener
    environments = environment_service.environments_service(vm_fixture.namespace)
    environment_route = relay["platform_routes"]["routes"]["environments"]
    assert environment_route["upstream"] == environments.authority
    assert environment_route["transport"] == {"mode": "tls_connect", "server_uri": SANDBOX_CONTROLLER_URI}
    sessions = runner_gateway.sessions_service(vm_fixture.namespace)
    sessions_authority = sessions.authority
    assert harness_env["AGENTPLANE_SESSIONS_URL"] == f"http://{sessions_authority}"
    assert relay["workload_routes"][sessions_authority] == {
        "upstream": sessions_authority,
        "server_uri": SESSION_DISPATCHER_URI,
    }
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


@pytest.mark.parametrize("location", ["K3S_FIXTURE_RLOCATION", "K3S_CENTRAL_FIXTURE_RLOCATION"])
def test_central_sampler_fixture_renders_app_ingress_gateway_and_history_wiring(
    runfiles: Runfiles, location: str
) -> None:
    fixture = _filled_fixture(_read_fixture(runfiles, location))
    assert fixture.central_sampler
    assert fixture.runtime is not rendered_fixture.SandboxRuntime.KUBEVIRT
    docs = fixture.objects

    app_config = yaml.safe_load(_resource(docs, "ConfigMap", "app-config")["data"]["config.yaml"])
    catalog = ModelCatalog.model_validate(app_config["models"])
    assert "central_sampler" not in app_config["models"]
    [selected_model] = catalog.harnesses[Harness.CENTRAL_SAMPLER]

    deployment = _resource(docs, "Deployment", central_sampler.NAME)
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == central_sampler.NAME
    container = next(container for container in pod["containers"] if container["name"] == central_sampler.NAME)
    service = _resource(docs, "Service", central_sampler.NAME)
    assert service["spec"]["selector"]
    assert all(
        deployment["spec"]["template"]["metadata"]["labels"][key] == value
        for key, value in service["spec"]["selector"].items()
    )
    service_ports = {port["name"]: port for port in service["spec"]["ports"]}
    container_ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    assert set(service_ports) == {"grpc-tls"}
    assert service_ports["grpc-tls"]["targetPort"] == container_ports["grpc-tls"]
    assert container["startupProbe"]["grpc"]["port"] == container_ports["health"]
    database_env = next(
        entry for entry in container["env"] if entry["name"] == env_name(CentralSamplerSettings, "database_url")
    )
    assert database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-central-sampler"
    settings = CentralSamplerSettings.model_validate_json(
        _resource(docs, "ConfigMap", f"{central_sampler.NAME}-settings")["data"]["settings.json"]
    )
    assert settings.executor_reconnect_timeout_seconds is None
    assert settings.health_listen == f"0.0.0.0:{container_ports['health']}"
    assert settings.actions is not None
    assert settings.actions.target == fixture_chart.ACTIONS_SERVICE.authority
    assert settings.actions.tls is not None
    assert settings.actions.tls.cert_file == "/run/agentplane/workload-tls/tls.crt"
    assert settings.actions.tls.key_file == "/run/agentplane/workload-tls/tls.key"
    assert settings.actions.tls.ca_file == "/run/agentplane/workload-trust/ca-certificates.crt"
    action_settings = yaml.safe_load(_resource(docs, "ConfigMap", "actions-settings")["data"]["settings.yaml"])
    assert action_settings["session_delegation_service"] == CENTRAL_SAMPLER_URI
    ingress_connection = settings.connections["llm-ingress"]
    assert ingress_connection.base_url == (f"https://{llm_ingress.service(NAMESPACE).authority}")
    assert ingress_connection.auth.type == "agentplane_ingress"
    assert ingress_connection.auth.credential_file is None
    assert ingress_connection.auth.credential is None
    assert ingress_connection.tls is not None
    assert str(ingress_connection.tls.ca_file) == "/run/agentplane/workload-trust/ca-certificates.crt"
    assert ingress_connection.tls.server_uri == LLM_INGRESS_URI
    assert ingress_connection.tls.client_uri == CENTRAL_SAMPLER_URI
    assert str(ingress_connection.tls.cert_file) == settings.actions.tls.cert_file
    assert str(ingress_connection.tls.key_file) == settings.actions.tls.key_file
    model = settings.models[selected_model]
    assert model.connection == "llm-ingress"
    assert model.provider_model == selected_model
    assert model.api == "responses"
    database_uri = urlsplit(_resource(docs, "Secret", "postgres-central-sampler")["stringData"]["uri"])
    assert database_uri.username == "central_sampler"
    assert database_uri.path == "/central_sampler"
    assert database_uri.hostname == f"{fixture_database.SERVICE}.{NAMESPACE}.svc"

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
    assert llm_config["central_sampler_identity_callers"] is True
    assert llm_config["central_sampler_services"] == [CENTRAL_SAMPLER_URI]
    assert {mode["mode"] for mode in llm_config["rpc"]["auth"]} == {"spiffe"}
    assert AnyHttpUrl(llm_config["litellm_url"]) == AnyHttpUrl(_TEST_LOCAL_MODEL.url)
    assert llm_config["log_levels"] == {}
    assert "litellm_key" not in llm_config
    gateway_config = yaml.safe_load(_resource(docs, "ConfigMap", "sessions-settings")["data"]["settings.yaml"])
    sampler_target = gateway_config["central_sampler"]
    assert sampler_target["target"] == central_sampler.service(NAMESPACE).authority
    assert "token_file" not in sampler_target
    assert sampler_target["tls"]["server_uri"] == CENTRAL_SAMPLER_URI
    assert sampler_target["tls"]["client_uri"] == GATEWAY_SESSIONS_URI
    for field in ("cert_file", "key_file", "ca_file"):
        assert sampler_target["tls"][field] == gateway_config["gateway_tls"][field]
    sampler_certificate = _resource(docs, "Certificate", f"{central_sampler.NAME}-workload")["spec"]
    controller = yaml.safe_load(_resource(docs, "ConfigMap", "environments-settings")["data"]["config.yaml"])
    assert set(sampler_certificate["uris"]) <= set(
        controller["environment_service"]["runtime_proxy"]["executor_callers"]
    )
    history_config = yaml.safe_load(_resource(docs, "ConfigMap", "history-service-config")["data"]["config.yaml"])
    assert history_config["migration_timeout_s"] == 600
    assert history_config["fold_concurrency"] == 4
    assert history_config["native_frame_read_batch_size"] == 200
    assert "database_url" not in history_config
    assert "log_levels" not in history_config
    assert sampler_certificate["uris"] == [history_config["ingress"]["sampler_service_actor"]]
    history_pod = _resource(docs, "Deployment", "history-service")["spec"]["template"]["spec"]
    history_container = history_pod["containers"][0]
    history_env = {entry["name"]: entry for entry in history_container["env"]}
    assert history_env["AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"]["value"] == (
        "/etc/agentplane-history-service/config.yaml"
    )
    history_database_env = history_env["AGENTPLANE_HISTORY_SERVICE_DATABASE_URL"]
    assert history_database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-sandbox-service"


def test_central_recovery_keeps_native_services_without_oauth_fixture_workloads(
    runfiles: Runfiles, mcp_fixture: rendered_fixture.RenderedFixture
) -> None:
    central = _filled_fixture(_read_fixture(runfiles, "K3S_CENTRAL_FIXTURE_RLOCATION"))
    assert central.mcp_fixtures is None
    assert central.operator_tokens is not None
    central_deployments = {doc["metadata"]["name"] for doc in central.objects if doc["kind"] == "Deployment"}
    mcp_deployments = {doc["metadata"]["name"] for doc in mcp_fixture.objects if doc["kind"] == "Deployment"}
    assert central_deployments == mcp_deployments - {"dex", "mcp-everything", "oauth-fixture"}

    # This preset takes the existing test-operator issuer path. The full MCP fixture gets its
    # operator token from Dex instead, while the recovery services retain identical config.
    central_app = yaml.safe_load(_resource(central.objects, "ConfigMap", "app-config")["data"]["config.yaml"])
    mcp_app = yaml.safe_load(_resource(mcp_fixture.objects, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert central_app["oidc"]["issuer"] == central.operator_tokens.issuer
    assert central_app["oidc"]["static_ed25519_keys"] == ["test-operator-public-key"]
    assert mcp_app["oidc"]["issuer"] == fixture_chart.DEX_ISSUER
    assert "static_ed25519_keys" not in mcp_app["oidc"]
    for component in ("central-sampler", "sessions", "history-service"):
        for kind in ("Deployment", "Service"):
            assert _resource(central.objects, kind, component) == _resource(mcp_fixture.objects, kind, component)
    for config in ("central-sampler-settings", "sessions-settings", "history-service-config"):
        assert _resource(central.objects, "ConfigMap", config) == _resource(mcp_fixture.objects, "ConfigMap", config)


@pytest.mark.parametrize(
    ("location", "central_enabled"),
    [("K3S_CENTRAL_NATIVE_FIXTURE_RLOCATION", True), ("K3S_POD_NATIVE_FIXTURE_RLOCATION", False)],
)
def test_native_recovery_profile_uses_service_caller_without_app(
    runfiles: Runfiles, location: str, central_enabled: bool
) -> None:
    fixture = _read_fixture(runfiles, location)
    assert fixture.central_sampler is central_enabled
    assert fixture.app_origin is None
    assert fixture.operator_tokens is None
    assert fixture.mcp_fixtures is None
    assert fixture.browser_services == []
    docs = fixture.objects
    deployments = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Deployment"}
    assert {"sessions", "history-service", "notifications"} <= deployments
    assert ("central-sampler" in deployments) is central_enabled
    assert not {"app", "dex", "mcp-edge"} & deployments
    assert not any(doc["metadata"]["name"] in {"postgres-app", "agentplane-oidc", "app-config"} for doc in docs)
    native = fixture_chart.NATIVE_TEST_CLIENT
    pod = _resource(docs, "Deployment", native)["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == native
    assert pod["automountServiceAccountToken"] is False
    certificate = _resource(docs, "Certificate", "app-workload")["spec"]
    assert certificate["uris"] == ["spiffe://agentplane/app"]
    assert any(volume.get("secret", {}).get("secretName") == certificate["secretName"] for volume in pod["volumes"])
    gateway = yaml.safe_load(_resource(docs, "ConfigMap", "sessions-settings")["data"]["settings.yaml"])
    controller = yaml.safe_load(_resource(docs, "ConfigMap", "environments-settings")["data"]["config.yaml"])
    assert set(certificate["uris"]) <= set(gateway["dispatcher_service_callers"])
    assert set(certificate["uris"]) <= set(controller["environment_service"]["service_readers"])
    proxy_readers = set(controller["environment_service"]["runtime_proxy"]["readers"])
    assert set(certificate["uris"]).isdisjoint(proxy_readers)
    runtime_proxy_test_reader = _resource(docs, "Certificate", fixture_chart.RUNTIME_PROXY_TEST_READER_CERTIFICATE)[
        "spec"
    ]
    assert runtime_proxy_test_reader["uris"] == [fixture_chart.RUNTIME_PROXY_TEST_READER_URI]
    assert proxy_readers == set(runtime_proxy_test_reader["uris"])
    notifications_certificate = _resource(docs, "Certificate", "notifications-workload")["spec"]
    assert notifications_certificate["uris"] == ["spiffe://agentplane/notifications"]
    assert set(notifications_certificate["uris"]).isdisjoint(proxy_readers)
    history = yaml.safe_load(_resource(docs, "ConfigMap", "history-service-config")["data"]["config.yaml"])
    assert set(certificate["uris"]) <= set(history["reader_service_callers"])
    bootstrap = _resource(docs, "ConfigMap", "postgres-bootstrap")["data"]["bootstrap.sql"]
    assert "CREATE ROLE app " not in bootstrap


def test_mcp_services_fixture_omits_sampler_and_keeps_app_and_oauth(
    runfiles: Runfiles, mcp_fixture: rendered_fixture.RenderedFixture
) -> None:
    services = _read_fixture(runfiles, "K3S_MCP_SERVICES_FIXTURE_RLOCATION")
    assert not services.central_sampler
    assert services.app_origin == mcp_fixture.app_origin
    assert services.mcp_fixtures == mcp_fixture.mcp_fixtures
    full_deployments = {doc["metadata"]["name"] for doc in mcp_fixture.objects if doc["kind"] == "Deployment"}
    deployments = {doc["metadata"]["name"] for doc in services.objects if doc["kind"] == "Deployment"}
    assert deployments == full_deployments - {"central-sampler"}
    assert {"app", "mcp-edge", "dex", "oauth-fixture", "mcp-everything"} <= deployments
    assert not any(doc["metadata"]["name"] == "postgres-central-sampler" for doc in services.objects)
    bootstrap = _resource(services.objects, "ConfigMap", "postgres-bootstrap")["data"]["bootstrap.sql"]
    assert "CREATE ROLE central_sampler " not in bootstrap


def test_firecracker_fixture_uses_production_construct_and_discloses_cilium_boundary(runfiles: Runfiles) -> None:
    fixture = _read_fixture(runfiles, "K3S_FIRECRACKER_FIXTURE_RLOCATION")
    assert fixture.runtime is rendered_fixture.SandboxRuntime.FIRECRACKER
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
