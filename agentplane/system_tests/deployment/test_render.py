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
from pydantic import AnyHttpUrl
from python.runfiles import Runfiles

# Each service module exports `Settings`; aliases keep their separate config contracts explicit.
from agentplane.app.settings import Settings as AppSettings
from agentplane.central_sampler.settings import Settings as CentralSamplerSettings
from agentplane.env.runtime.harness import Harness
from agentplane.mcp_edge.oauth_settings import OAuthSettings
from agentplane.mcp_edge.settings import Settings as McpEdgeSettings
from agentplane.runner_gateway.settings import Settings
from agentplane.sandboxes.runtimeconfig.model_catalog import ModelCatalog
from agentplane.system_tests.deployment import fixture_chart, fixture_database, rendered_fixture
from agentplane.system_tests.deployment.rendered_fixture import LocalModel, McpInputs, RuntimeInputs
from agentplane.system_tests.k3s.namespaces import NAMESPACE
from agentplane.system_tests.vm import infrastructure as vm_infrastructure, sandboxes as vm_sandboxes
from agentplane.system_tests.vm.block_storage import STORAGE_CLASS
from agentplane.system_tests.vm.configuration import OOM_TEMPLATE, SUPERVISION_KERNEL_ARG, SUPERVISION_TEMPLATE
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s.agentplane import (
    central_sampler,
    environment_firecracker,
    environment_vm,
    history_service,
    llm_ingress,
    runner_gateway,
)
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


def _filled_fixture(runfiles: Runfiles) -> rendered_fixture.RenderedFixture:
    cached = rendered_fixture.read(runfiles.root() / os.environ["K3S_FIXTURE_RLOCATION"])
    return rendered_fixture.fill(
        cached,
        RuntimeInputs(
            ca_cert="fixture-ca\n", ca_key="fixture-key\n", system_roots="system-roots\n", cluster_ca="cluster-ca\n"
        ),
        LocalModel(url="http://scripted-model.k3s-fixture.invalid:4000"),
        _MCP,
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
    app_pod = _resource(docs, "Deployment", "app")["spec"]["template"]["spec"]
    app_container = app_pod["containers"][0]
    assert app_container["startupProbe"]["httpGet"]["path"] == "/healthz"
    actions_container = _resource(docs, "Deployment", "actions")["spec"]["template"]["spec"]["containers"][0]
    assert actions_container["readinessProbe"]["grpc"] == {"port": fixture_chart.ACTIONS_SERVICE.pod_port}
    assert actions_container["livenessProbe"]["grpc"] == {
        "port": fixture_chart.ACTIONS_SERVICE.pod_port,
        "service": "livez",
    }
    for workload in ("actions", "notifications"):
        pod = _resource(docs, "Deployment", workload)["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert container["startupProbe"]["grpc"] == {
            "port": container["readinessProbe"]["grpc"]["port"],
            "service": "livez",
        }
    edge_pod = _resource(docs, "Deployment", "mcp-edge")["spec"]["template"]["spec"]
    assert not edge_pod.get("initContainers")
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
    assert edge_database_env["valueFrom"]["secretKeyRef"]["name"] == "postgres-mcp-edge"
    egress_pod = _resource(docs, "Deployment", "egress")["spec"]["template"]["spec"]
    proxy = next(container for container in egress_pod["containers"] if container["name"] == "proxy")
    egress_config = yaml.safe_load(_resource(docs, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"])
    assert egress_config["forward"]["listen_address"] == "0.0.0.0:8888"
    proxy_certificate = _resource(docs, "Certificate", "agentplane-egress-proxy")["spec"]
    assert proxy_certificate["uris"] == ["spiffe://agentplane/egress-proxy"]
    assert proxy_certificate["secretName"] == "agentplane-egress-proxy-tls"
    assert proxy_certificate["issuerRef"] == {"name": "agentplane-runner-ca", "kind": "Issuer"}
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
    gateway_env = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert gateway_env[env_name(Settings, "runner_token_file")] == f"{token_mount['mountPath']}/{token['path']}"
    assert gateway_env[env_name(Settings, "gateway_audience")] == token["audience"]
    gateway_config = yaml.safe_load(
        _resource(docs, "ConfigMap", f"{fixture_chart.RUNNER_GATEWAY}-settings")["data"]["settings.yaml"]
    )
    assert gateway_config["operator_callers"] == [f"{NAMESPACE}/app"]
    assert gateway_config["environment_launchers"] == [f"{NAMESPACE}/actions"]
    assert gateway_config["workload"]["listen_address"].startswith("0.0.0.0:")
    assert gateway_config["workload"]["tls"] == {
        "cert_file": "/run/agentplane/workload-tls/tls.crt",
        "key_file": "/run/agentplane/workload-tls/tls.key",
    }
    assert gateway_config["workload"]["auth"] == [
        {"mode": "spiffe", "ca_file": "/run/agentplane/workload-trust/ca-certificates.crt"}
    ]
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
    assert int(gateway_config["workload"]["listen_address"].rsplit(":", 1)[1]) == gateway_listener_ports["workload"]
    for name in (fixture_chart.RUNNER_GATEWAY, fixture_chart.SESSION_DISPATCHER):
        service = _resource(docs, "Service", name)
        assert service["spec"]["selector"]
        assert all(gateway_pod_labels[key] == value for key, value in service["spec"]["selector"].items())
        assert all(port["targetPort"] == gateway_listener_ports[port["name"]] for port in service["spec"]["ports"])

    assert int(gateway_env[env_name(Settings, "listen_address")].rsplit(":", 1)[1]) == gateway_listener_ports["grpc"]

    dispatcher = _resource(docs, "Service", fixture_chart.SESSION_DISPATCHER)
    dispatcher_port = next(port["port"] for port in dispatcher["spec"]["ports"] if port["name"] == "grpc")
    endpoint = f"{dispatcher['metadata']['name']}.{NAMESPACE}.svc.cluster.local:{dispatcher_port}"
    app = _resource(docs, "Deployment", "app")
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
    telemetry_route = sidecar_settings["platform_routes"]["telemetry"]
    routes = sidecar_settings["platform_routes"]
    addresses = [route["listen_address"] for route in routes.values()]
    assert len(addresses) == len(set(addresses)), "platform routes must not compete for a listener"
    runner_config = json.loads(
        _resource(docs, "ConfigMap", f"{fixture_chart.POD_TEMPLATE}-config")["data"]["environment-runtime.json"]
    )
    assert runner_config["model_config_service"]["target"] == routes["model-config"]["listen_address"]
    runtime = next(
        container
        for container in template["spec"]["pod"]["spec"]["containers"]
        if container["name"] == "environment-runtime"
    )
    runtime_env = {entry["name"]: entry["value"] for entry in runtime["env"] if "value" in entry}
    assert runtime_env["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"http://{telemetry_route['listen_address']}"
    assert runtime_env.get("OTEL_SDK_DISABLED") != "true"
    sidecar_env = {entry["name"]: entry["value"] for entry in runner_sidecar[0]["env"] if "value" in entry}
    assert sidecar_env["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"http://{telemetry_route['upstream']}"
    assert sidecar_env.get("OTEL_SDK_DISABLED") != "true"
    assert telemetry_route["upstream"] == "alloy.monitoring.svc.cluster.local:4317"
    runner_pod_spec = template["spec"]["pod"]["spec"]
    runner_egress_volume = next(v for v in runner_pod_spec["volumes"] if v["name"] == "egress-token")
    runner_token_sources = runner_egress_volume["projected"]["sources"]
    assert not any(
        source.get("serviceAccountToken", {}).get("audience") == "agentplane-egress" for source in runner_token_sources
    )
    rules_route = sidecar_settings["platform_routes"]["egress-rules"]
    assert rules_route["transport"] == {"mode": "tls_connect", "server_uri": "spiffe://agentplane/egress-proxy"}
    assert rules_route["upstream"] in egress_config["tls_passthrough_targets"]
    assert runtime_env["AGENTPLANE_EGRESS_GRPC_URL"] == f"http://{rules_route['listen_address']}"
    assert "AGENTPLANE_EGRESS_GRPC_CREDENTIAL" not in runtime_env
    assert sidecar_settings["proxy"]["transport"]["mode"] == "tls"
    assert sidecar_settings["proxy"]["transport"]["auth"]["mode"] == "spiffe"
    for name in ("sandbox", "sandbox-build"):
        pod_template = _resource(docs, "PodTemplate", name)
        command_pod = pod_template["template"]["spec"]
        assert command_pod["automountServiceAccountToken"] is False
        assert not command_pod.get("initContainers")
        assert len(command_pod["containers"]) == 1
        assert {volume["name"] for volume in command_pod["volumes"]}.isdisjoint(
            {"egress-token", "egress-sidecar-settings"}
        )
        env_names = {entry["name"].lower() for entry in command_pod["containers"][0]["env"]}
        assert env_names.isdisjoint({"http_proxy", "https_proxy", "no_proxy", "grpc_proxy"})
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
    chart_service_accounts = {
        f"{doc['metadata']['namespace']}/{doc['metadata']['name']}": doc
        for doc in docs
        if doc["kind"] == "ServiceAccount"
    }
    static_account = f"{fixture.namespace}/mcp-edge-test-caller"
    sandbox_account = f"{fixture.namespace}/systest-delegation-caller"
    assert set(mintable_accounts) == {static_account, sandbox_account}
    # The static OAuth fixture caller is chart-owned and must remain labeled for Actions.
    assert set(mintable_accounts) & chart_service_accounts.keys() == {static_account}
    caller = chart_service_accounts[static_account]
    assert caller["metadata"]["labels"]["agentplane.allegedly.works/use-action-service"] == "true"
    # The Sandbox controller creates and labels this account after chart rendering.
    token_rules = [
        rule for rule in _resource(docs, "Role", "mcp-edge")["rules"] if "serviceaccounts/token" in rule["resources"]
    ]
    assert {name for rule in token_rules for name in rule["resourceNames"]} == {
        account.split("/")[1] for account in mintable_accounts
    }


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
    relay_address = relay["platform_routes"]["model-config"]["listen_address"]
    assert config["model_config_service"]["target"].rsplit(":", 1)[1] == relay_address.rsplit(":", 1)[1]
    assert config["harness_environment"]["values"]["AGENTPLANE_EGRESS_GRPC_URL"] == "http://10.0.2.1:3133"
    assert config["harness_environment"]["values"]["AGENTPLANE_NOTIFICATIONS_GRPC_URL"] == "http://10.0.2.1:3134"
    assert "AGENTPLANE_NOTIFICATIONS_GRPC_CREDENTIAL" not in config["harness_environment"]["values"]
    assert "AGENTPLANE_EGRESS_GRPC_CREDENTIAL" not in config["harness_environment"]["values"]
    oom_config_map = _resource(vm_fixture.objects, "ConfigMap", oom_config_volume["configMap"]["name"])
    oom_config = json.loads(oom_config_map["data"]["environment-runtime.json"])
    oom_resources = oom_config["environment"]["resources"]
    ordinary_resources = config["environment"]["resources"]
    oom_executor = oom_resources["executor"]
    ordinary_executor = ordinary_resources["executor"]
    assert oom_executor["memory_max_bytes"] < ordinary_executor["memory_max_bytes"]
    assert {
        **oom_resources,
        "executor": {**oom_executor, "memory_max_bytes": ordinary_executor["memory_max_bytes"]},
    } == ordinary_resources
    control = config["control"]
    assert {"port": int(control["listen_address"].rsplit(":", 1)[1])} in interface["ports"]
    assert control["security"] == {
        "mode": "mtls",
        "tls": {
            "cert_file": "/run/agentplane/environment-runtime-tls/tls.crt",
            "key_file": "/run/agentplane/environment-runtime-tls/tls.key",
            "ca_file": "/run/agentplane/environment-runtime-trust/ca-certificates.crt",
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
    assert ingress_connection.base_url == llm_ingress.proxied_url(NAMESPACE)
    assert ingress_connection.auth.type == "bearer"
    assert str(ingress_connection.auth.credential_file) == "/var/run/secrets/central-sampler/llm"
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

    llm_config = yaml.safe_load(_resource(docs, "ConfigMap", "llm-ingress-settings")["data"]["settings.yaml"])
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
    assert {"namespace": NAMESPACE, "name": central_sampler.NAME} in history_config["ingress"]["sampler_accounts"]


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
