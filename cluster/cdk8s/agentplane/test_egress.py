"""What the namespace grants a sandbox: SVID-protected service routes and Kubernetes access."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.crd_group import GROUP
from agentplane.egress.kubernetes_audience import KUBERNETES_AUDIENCE
from agentplane.egress.settings import Settings as EgressSettings
from agentplane.environment_runtime_transport.config import LLM_INGRESS_URI, SESSION_DISPATCHER_URI
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import agent_access_profiles
from cluster.cdk8s.agentplane import (
    binding_delegation,
    environment_sandbox,
    llm_ingress,
    notifications,
    runner_gateway,
    session_dispatcher,
    staging,
    staging_grants,
)
from cluster.cdk8s.agentplane.egress import (
    ADMIN_GRPC_AUDIENCE,
    BASIC_POLICY,
    DEFAULT_POLICIES,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    INFERENCE_EXPERIMENTS_POLICY,
    KUBERNETES_CREDENTIAL,
    KUBERNETES_HOST,
    PROXY_PORT,
    PUBLIC_INTERNET_POLICY,
    Egress,
    admin,
)
from cluster.cdk8s.agentplane.egress_staging_credentials import AIQUOTA_READ_POLICY, PLAID_PGWEB_CREDENTIAL
from cluster.cdk8s.agentplane.model_selections import STAGING_APP_MODELS
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.secret_ref import SecretRef
from model_catalog.catalog import Provider


def _by_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """The one object of `kind` named `name`, which every test here asserts exists."""
    return one(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _environment_default_policies(docs: list[dict[str, Any]]) -> list[str]:
    """The PolicySets EnvironmentService applies to each new Sandbox."""
    config = _by_name(docs, "ConfigMap", "runner-gateway-settings")
    defaults: list[str] = yaml.safe_load(config["data"]["settings.yaml"])["environment_default_policy_sets"]
    return defaults


def test_egress_uses_embedded_migrations_and_startup_probe(catalog_manifests: list[dict[str, Any]]) -> None:
    pod = _by_name(catalog_manifests, "Deployment", "egress")["spec"]["template"]["spec"]
    proxy = one(container for container in pod["containers"] if container["name"] == "proxy")
    assert not pod.get("initContainers")
    startup = proxy["startupProbe"]["exec"]["command"]
    readiness = proxy["readinessProbe"]["exec"]["command"]
    assert startup[:2] == readiness[:2] == ["/healthprobe", "--target"]
    assert "127.0.0.1:8081" in startup
    assert "spiffe://agentplane/egress-proxy" in startup
    assert "--service" in startup
    assert "livez" in startup
    proxy_env = {entry["name"]: entry for entry in proxy["env"]}
    assert "PGSSLMODE" not in proxy_env
    settings_path = PurePosixPath(proxy_env[EgressSettings.config_file_env]["value"])
    settings_mount = one(mount for mount in proxy["volumeMounts"] if mount["mountPath"] == str(settings_path.parent))
    settings_volume = one(volume for volume in pod["volumes"] if volume["name"] == settings_mount["name"])
    assert settings_volume["configMap"]["name"] == "egress-go-settings"
    assert settings_path.name in _by_name(catalog_manifests, "ConfigMap", "egress-go-settings")["data"]
    assert "args" not in proxy
    assert proxy_env["AGENTPLANE_EGRESS_RULES_NAMESPACE"]["value"] == staging.ENV.namespace
    config = yaml.safe_load(_by_name(catalog_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"])
    assert config["admin_grpc_audience"] == ADMIN_GRPC_AUDIENCE
    assert config["admin_grpc_callers"] == [f"{staging.ENV.namespace}/app"]


@pytest.fixture(scope="module")
def svid_egress_manifests(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    app = scratch_app_factory()
    chart = Chart(app, "svid-egress")
    env = staging.ENV
    Egress(
        chart,
        "egress",
        env,
        database_url=SecretRef(namespace=env.namespace, name="postgres-egress")
        .key("uri")
        .env_value(chart, "test-database-url"),
        runners=environment_sandbox.pods(env.namespace),
        admin_grpc_callers=[ServiceAccountRef(namespace=env.namespace, name="app")],
        placement=None,
        registry_secret_name=None,
    ).add_platform_resources()
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))


def test_staging_requires_the_tls_hop_and_rules_boundary(catalog_manifests: list[dict[str, Any]]) -> None:
    deployment = _by_name(catalog_manifests, "Deployment", "egress")
    proxy = one(
        container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "proxy"
    )
    ports = {port["containerPort"] for port in proxy["ports"]}
    assert PROXY_PORT in ports
    assert admin(staging.ENV.namespace).pod_port in ports
    settings = yaml.safe_load(_by_name(catalog_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"])
    assert settings["forward"]["listen_address"] == f"0.0.0.0:{PROXY_PORT}"
    assert {method["mode"] for method in settings["forward"]["auth"]} == {"spiffe", "kubernetes_bearer"}
    api = admin(staging.ENV.namespace)
    assert f"{api.fqdn}:{api.port.number}" in settings["tls_passthrough_targets"]


def test_tls_hop_and_rules_add_only_bound_sandbox_readers_and_tls_ports(
    svid_egress_manifests: list[dict[str, Any]],
) -> None:
    deployment = _by_name(svid_egress_manifests, "Deployment", "egress")
    proxy = one(
        container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "proxy"
    )
    assert [port["containerPort"] for port in proxy["ports"]].count(PROXY_PORT) == 1
    service = _by_name(svid_egress_manifests, "Service", "egress")
    mtls_ports = [port for port in service["spec"]["ports"] if port["port"] == PROXY_PORT]
    assert len(mtls_ports) == 1

    settings = yaml.safe_load(
        _by_name(svid_egress_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"]
    )
    assert settings["forward"]["listen_address"] == f"0.0.0.0:{PROXY_PORT}"
    assert settings["forward"]["auth"][0]["ca_file"] == "/etc/agentplane-egress/svid-ca/ca-certificates.crt"

    role = _by_name(svid_egress_manifests, "Role", "egress")
    sandbox_rules = [rule for rule in role["rules"] if "sandboxes" in rule["resources"]]
    assert sandbox_rules == [{"apiGroups": [GROUP], "resources": ["sandboxes"], "verbs": ["get", "list", "watch"]}]
    all_resources = [resource for rule in role["rules"] for resource in rule["resources"]]
    assert set(all_resources).isdisjoint({"pods", "serviceaccounts"})

    certificate = _by_name(svid_egress_manifests, "Certificate", "agentplane-egress-proxy")
    assert certificate["spec"]["uris"] == ["spiffe://agentplane/egress-proxy"]
    assert "server auth" in certificate["spec"]["usages"]

    policy = _by_name(svid_egress_manifests, "CiliumNetworkPolicy", "egress")["spec"]
    ingress_ports = {
        int(port["port"])
        for rule in policy["ingress"]
        for port_rule in rule.get("toPorts", [])
        for port in port_rule["ports"]
    }
    assert PROXY_PORT in ingress_ports


def test_app_projects_egress_admin_audience(catalog_manifests: list[dict[str, Any]]) -> None:
    pod = _by_name(catalog_manifests, "Deployment", "app")["spec"]["template"]["spec"]
    container = one(pod["containers"])
    volume = one(volume for volume in pod["volumes"] if volume["name"] == "egress-admin-token")
    (token,) = volume["projected"]["sources"]
    assert token["serviceAccountToken"]["audience"] == ADMIN_GRPC_AUDIENCE
    assert token["serviceAccountToken"]["path"] == "token"
    assert {mount["name"]: mount["mountPath"] for mount in container["volumeMounts"]}[
        "egress-admin-token"
    ] == "/var/run/secrets/agentplane-egress-admin"


def test_environment_default_policy_sets_are_canonical(catalog_manifests: list[dict[str, Any]]) -> None:
    app_defaults = _environment_default_policies(catalog_manifests)
    assert app_defaults == sorted(DEFAULT_POLICIES)
    assert PUBLIC_INTERNET_POLICY not in app_defaults


def test_basic_policy_auto_approves_only_same_principal_session_actions(
    catalog_manifests: list[dict[str, Any]],
) -> None:
    basic = _by_name(catalog_manifests, "PolicySet", BASIC_POLICY)
    action_rules = [rule for rule in basic["spec"]["rules"] if rule["type"] == "action_auto_approve"]
    assert [rule["policy"] for rule in action_rules] == [
        {
            "type": "exact_actions",
            "actions": {
                "environments": ["get", "list", "resume"],
                "sessions": [
                    "cancel_input",
                    "create",
                    "get",
                    "interrupt_turn",
                    "observe_input",
                    "read_turns",
                    "submit_input",
                ],
            },
        }
    ]

    # `basic` is both a fleet default and explicitly held by the static MCP/workload principals;
    # ordinary preset choices can replace other policies but still include it.
    assert BASIC_POLICY in _environment_default_policies(catalog_manifests)
    for account in ("claude-ai", "haku-agent"):
        binding = _by_name(catalog_manifests, "PolicyBinding", account)
        assert BASIC_POLICY in binding["spec"]["policySets"]
    app_config = yaml.safe_load(_by_name(catalog_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert app_config["sandbox_presets"]
    for preset in app_config["sandbox_presets"].values():
        assert BASIC_POLICY in preset["policy_sets"]


def test_public_internet_is_available_but_never_implicitly_granted(catalog_manifests: list[dict[str, Any]]) -> None:
    public = _by_name(catalog_manifests, "PolicySet", PUBLIC_INTERNET_POLICY)
    assert public["spec"]["rules"] == [{"type": "egress", "hosts": ["*"]}]
    # Every preset, not just today's public-coder preset, and the defaults.
    assert PUBLIC_INTERNET_POLICY not in _by_name(catalog_manifests, "ConfigMap", "app-config")["data"]["config.yaml"]
    assert PUBLIC_INTERNET_POLICY not in _environment_default_policies(catalog_manifests)
    for doc in catalog_manifests:
        if doc["kind"] == "PolicyBinding":
            if doc["metadata"]["name"] == "public-coder-openclaw":
                assert doc["spec"]["subject"] in (
                    {"namespace": "public-coder-agent", "name": "openclaw"},
                    {"identity": {"name": "public-coder-openclaw"}},
                )
                assert doc["spec"]["policySets"] == [
                    "public-coder-openclaw",
                    INFERENCE_EXPERIMENTS_POLICY,
                    PUBLIC_INTERNET_POLICY,
                ]
            else:
                assert PUBLIC_INTERNET_POLICY not in doc["spec"]["policySets"]


def test_kubernetes_access_is_part_of_what_every_sandbox_is_granted(staging_manifests: list[dict[str, Any]]) -> None:
    """Every agent talks to the API server, so the rule admitting it is in `basic`.

    `basic` is the policy every launch is granted before the caller picks anything
    (`SandboxAccess`, agentplane/app/sandbox_access.py), which is what makes
    Kubernetes access a property of a sandbox rather than a choice one. The credential description
    tells the agent which API server to use and that the proxy substitutes the calling Sandbox's
    own projected token; the SandboxTemplate does not materialize a kubeconfig.

    What makes it legitimate as an unconditional grant is what gets substituted. A default is the
    one grant a caller cannot decline, so it may only ever substitute the sandbox's own identity;
    this credential is that Pod's ServiceAccount projected for the API server's audience, and a
    `secretRef` source here would hand an operator's credential to every sandbox in the namespace.
    What a box may then do is the RBAC bound to its account, the only axis this narrows -- which
    is why the rule names neither methods nor paths.
    """
    basic = _by_name(staging_manifests, "PolicySet", BASIC_POLICY)
    api_server = one(rule for rule in basic["spec"]["rules"] if KUBERNETES_HOST in rule.get("hosts", []))
    assert api_server.get("credentialRef") == {"name": KUBERNETES_CREDENTIAL}, api_server
    # Narrowing by verb or path would be a second, weaker copy of RBAC; RBAC decides.
    assert "methods" not in api_server, api_server
    assert "paths" not in api_server, api_server

    credential = _by_name(staging_manifests, "EgressCredential", KUBERNETES_CREDENTIAL)
    description = credential["spec"]["description"]
    placeholder = f"agentplane-credential-{KUBERNETES_CREDENTIAL}"
    assert f"https://{KUBERNETES_HOST}" in description
    assert "calling Sandbox's own ServiceAccount" in description
    assert f"kubectl --server https://{KUBERNETES_HOST} --token {placeholder}" in description
    source = credential["spec"]["source"]
    assert "secretRef" not in source, "a default may substitute a sandbox's own credential, never an operator's"
    assert source["projectedWorkloadToken"]["audience"] == KUBERNETES_AUDIENCE, source

    # And `basic` reaches a sandbox that picks nothing, which is the whole claim.
    defaults = _environment_default_policies(staging_manifests)
    assert BASIC_POLICY in defaults, defaults


def test_notifications_verify_svids_behind_an_opaque_tunnel(catalog_manifests: list[dict[str, Any]]) -> None:
    policy = _by_name(catalog_manifests, "PolicySet", BASIC_POLICY)
    assert all(
        notifications.service(staging.ENV.namespace).fqdn not in rule.get("hosts", [])
        for rule in policy["spec"]["rules"]
    )
    assert not any(
        item["kind"] == "EgressCredential" and item["metadata"]["name"] == "agentplane-notifications-workload"
        for item in catalog_manifests
    )
    proxy_settings = yaml.safe_load(
        _by_name(catalog_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"]
    )
    assert notifications.TOKEN_AUDIENCE not in proxy_settings["projected_token_audiences"]
    endpoint = notifications.service(staging.ENV.namespace)
    assert f"{endpoint.fqdn}:{endpoint.pod_port}" in proxy_settings["tls_passthrough_targets"]
    notification_settings = yaml.safe_load(
        _by_name(catalog_manifests, "ConfigMap", f"{notifications.NAME}-settings")["data"]["settings.yaml"]
    )
    assert notification_settings["rpc"]["listen_address"] == f"0.0.0.0:{endpoint.pod_port}"
    assert notification_settings["rpc"]["tls"]["cert_file"].endswith("/tls.crt")


def test_llm_runner_traffic_uses_its_svid_listener(catalog_manifests: list[dict[str, Any]]) -> None:
    llm_service = _by_name(catalog_manifests, "Service", "llm-ingress")
    assert {port["port"] for port in llm_service["spec"]["ports"]} == {8080}

    settings = yaml.safe_load(_by_name(catalog_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"])
    workload = llm_ingress.service(staging.ENV.namespace)
    assert f"{workload.fqdn}:{workload.pod_port}" in settings["tls_passthrough_targets"]

    llm_settings = yaml.safe_load(
        _by_name(catalog_manifests, "ConfigMap", "llm-ingress-settings")["data"]["settings.yaml"]
    )
    assert llm_settings["rpc"]["listen_address"] == "0.0.0.0:8080"
    assert llm_settings["rpc"]["tls"]["cert_file"].endswith("/tls.crt")
    assert llm_settings["rpc"]["tls"]["key_file"].endswith("/tls.key")

    certificate = _by_name(catalog_manifests, "Certificate", "llm-ingress-workload")
    assert certificate["spec"]["uris"] == [LLM_INGRESS_URI]
    assert "server auth" in certificate["spec"]["usages"]

    role = _by_name(catalog_manifests, "Role", "llm-ingress-sandbox-reader")
    assert role["rules"] == [
        {"apiGroups": [GROUP], "resources": ["sandboxes"], "verbs": ["get", "list"]},
        {"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["get"]},
    ]

    basic_policy = _by_name(catalog_manifests, "PolicySet", BASIC_POLICY)
    allowed_hosts = [host for rule in basic_policy["spec"]["rules"] for host in rule.get("hosts", [])]
    assert llm_ingress.service(staging.ENV.namespace).fqdn not in allowed_hosts

    network_policy = _by_name(catalog_manifests, "CiliumNetworkPolicy", "llm-ingress")["spec"]
    assert any(
        int(port["port"]) == workload.pod_port
        for rule in network_policy["ingress"]
        for port_rule in rule.get("toPorts", [])
        for port in port_rule["ports"]
    )


def test_session_dispatcher_sandbox_calls_use_the_workload_svid_route(catalog_manifests: list[dict[str, Any]]) -> None:
    """Keep the CLI origin credential-free while the sidecar presents its SVID."""
    namespace = staging.ENV.namespace
    service_url = (
        f"http://{session_dispatcher.service(namespace).fqdn}:{session_dispatcher.service(namespace).port.number}"
    )
    destination = runner_gateway.service(namespace)
    dispatcher_origin = (
        f"{session_dispatcher.service(namespace).fqdn}:{session_dispatcher.service(namespace).port.number}"
    )
    basic = _by_name(catalog_manifests, "PolicySet", BASIC_POLICY)
    assert all(
        session_dispatcher.service(namespace).fqdn not in rule.get("hosts", []) for rule in basic["spec"]["rules"]
    )
    proxy_settings = yaml.safe_load(
        _by_name(catalog_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"]
    )
    assert session_dispatcher.TOKEN_AUDIENCE not in proxy_settings["projected_token_audiences"]
    assert f"{destination.fqdn}:{destination.pod_port}" in proxy_settings["tls_passthrough_targets"]

    configured: list[tuple[str, Any]] = []
    for item in catalog_manifests:
        if item["kind"] != "ConfigMap" or "settings.yaml" not in item.get("data", {}):
            continue
        routes = yaml.safe_load(item["data"]["settings.yaml"]).get("workload_routes", {})
        configured.extend((origin, routes[origin]) for origin in (dispatcher_origin,) if origin in routes)
    assert {origin for origin, _ in configured} == {dispatcher_origin}
    for _, route in configured:
        assert route["upstream"] == f"{destination.fqdn}:{destination.pod_port}"
        assert route["server_uri"] == SESSION_DISPATCHER_URI

    pod_template = _by_name(catalog_manifests, "SandboxTemplate", "environment-tools")
    pod_runner = one(
        container
        for container in pod_template["spec"]["pod"]["spec"]["containers"]
        if container["name"] == "environment-runtime"
    )
    pod_env = {entry["name"]: entry["value"] for entry in pod_runner["env"] if "value" in entry}
    assert pod_env["AGENTPLANE_SESSIONS_URL"] == service_url
    assert not any(name.startswith("AGENTPLANE_") and name.endswith("_CREDENTIAL") for name in pod_env)
    assert all(
        source.get("serviceAccountToken", {}).get("audience") != session_dispatcher.TOKEN_AUDIENCE
        for volume in pod_template["spec"]["pod"]["spec"]["volumes"]
        for source in volume.get("projected", {}).get("sources", [])
    )


def test_github_policy_admits_the_write_verbs_a_pr_lifecycle_needs(catalog_manifests: list[dict[str, Any]]) -> None:
    """The bot can finish a pull request, not only open one.

    GitHub answers a PR description with PATCH and `update-branch`/`merge` with PUT; a review comment
    or a merged head branch sits behind DELETE. POST alone leaves a sandbox able to file a change and
    unable to correct it. Pinned in both directions so that trimming a verb stays a decision rather
    than a side effect of tidying, and so the rule stays unpathed rather than narrowing quietly.
    """
    github = _by_name(catalog_manifests, "PolicySet", GITHUB_AGENTYDRAGON_AGENT_POLICY)
    rule = one(github["spec"]["rules"])
    assert set(rule["methods"]) == {"GET", "POST", "PUT", "PATCH", "DELETE"}
    assert rule["credentialRef"] == {"name": "github-pat"}
    # Unpathed: the verb list bounds the method, while the PAT behind it stays whole (egress.py).
    assert "paths" not in rule


def test_claude_picker_offers_environment_models_and_keeps_ingress(catalog_manifests: list[dict[str, Any]]) -> None:
    config = yaml.safe_load(_by_name(catalog_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert config["models"]["harnesses"]["HARNESS_CLAUDE"] == [route.id for route in STAGING_APP_MODELS.claude]
    assert all(
        route.id in config["models"]["harnesses"]["HARNESS_CENTRAL_SAMPLER"] for route in STAGING_APP_MODELS.claude
    )
    for preset in config["thread_presets"].values():
        assert preset["model"] in config["models"]["harnesses"][preset["harness"]]
    # The Anthropic Messages endpoint remains available to Claude sessions.
    assert any(doc["kind"] == "Service" and doc["metadata"]["name"] == "llm-ingress" for doc in catalog_manifests)


def test_shared_coding_models_preserve_provider_protocols(catalog_manifests: list[dict[str, Any]]) -> None:
    config = yaml.safe_load(_by_name(catalog_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    catalog = config["models"]
    assert catalog["harnesses"]["HARNESS_CODEX"] == [route.id for route in STAGING_APP_MODELS.codex]
    assert catalog["harnesses"]["HARNESS_CENTRAL_SAMPLER"] == [route.id for route in STAGING_APP_MODELS.central_sampler]
    assert set(catalog["harnesses"]["HARNESS_CODEX"]) < set(catalog["harnesses"]["HARNESS_CENTRAL_SAMPLER"])
    assert "central_sampler" not in catalog
    sampler = json.loads(_by_name(catalog_manifests, "ConfigMap", "central-sampler-settings")["data"]["settings.json"])
    assert set(sampler["models"]) == {route.id for route in STAGING_APP_MODELS.central_sampler}
    for route in STAGING_APP_MODELS.central_sampler:
        model = sampler["models"][route.id]
        if route.upstream.provider == Provider.ANTHROPIC_API:
            assert model["api"] == "anthropic_messages"
            assert model["connection"] == "anthropic-api"
            assert model["exec_encoding"] == "json"
        elif route.upstream.provider == Provider.CHATGPT:
            assert model["api"] == "responses"
            assert model["connection"] == "llm-ingress"
            assert model["responses"] == {"dialect": "lite"}
            assert model["exec_encoding"] == "freeform"
        else:
            assert model["api"] == "responses"
            assert model["connection"] == "llm-ingress"
            assert model["responses"] == {"dialect": "standard"}
            assert model["exec_encoding"] == "json"


def test_haku_launches_on_an_available_central_sampler_route(catalog_manifests: list[dict[str, Any]]) -> None:
    """Haku's model and effort must be accepted by the selected harness.

    Sandbox and Action launches use the same preset, so both must resolve to that harness and model.
    """
    config = yaml.safe_load(_by_name(catalog_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    preset = config["thread_presets"]["haku-codex"]
    assert preset["harness"] == "HARNESS_CENTRAL_SAMPLER"
    assert preset["model"] in config["models"]["harnesses"]["HARNESS_CENTRAL_SAMPLER"]
    option = one(model for model in config["models"]["models"] if model["model"] == preset["model"])
    assert preset["reasoning_effort"] in option["reasoning_efforts"]
    assert config["sandbox_presets"]["haku"]["thread_preset"] == "haku-codex"
    actions = yaml.safe_load(_by_name(catalog_manifests, "ConfigMap", "actions-settings")["data"]["settings.yaml"])
    action_spec = actions["action_groups"]["orchestration_v1"]["executor"]["presets"]["haku"]["create_session"]["spec"]
    assert action_spec["harness"] == preset["harness"]
    assert action_spec["model"] == preset["model"]
    assert "central_sampler" not in action_spec


def _kubernetes_grants(policy_sets: list[str]) -> set[str]:
    """The launch grants a preset's Kubernetes PolicySets hold."""
    return {grant for name in policy_sets for grant in agent_access_profiles.POLICY_SETS.get(name, ())}


@pytest.mark.parametrize("preset", ["public-coder", "finance-agent"])
def test_public_diagnostics_select_cluster_reader_and_share_haku_reads(
    preset: str, staging_manifests: list[dict[str, Any]]
) -> None:
    config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert "kubernetes_grants" not in config
    grant_models = staging_grants.catalog(staging.ENV.namespace)
    catalog = {name: grant.model_dump(mode="json") for name, grant in grant_models.items()}
    selected = _kubernetes_grants(config["sandbox_presets"][preset]["policy_sets"])
    credential_grants = (
        {"coinbase-credentials", "spend-policy-config", agent_access_profiles.PLAID_DIAGNOSTICS_POLICY}
        if preset == "finance-agent"
        else set()
    )
    # Coinbase bundles the Secret grant and GET egress; only explicitly selected callers get it.
    assert (agent_access_profiles.COINBASE_POLICY in config["sandbox_presets"][preset]["policy_sets"]) == (
        preset == "finance-agent"
    )
    assert selected == _kubernetes_grants(config["sandbox_presets"]["public-coder"]["policy_sets"]) | credential_grants
    assert "cluster-diagnostics" in selected
    haku_policy_sets = config["sandbox_presets"]["haku"]["policy_sets"]
    haku = _kubernetes_grants(haku_policy_sets)
    assert "cluster-diagnostics" in haku
    assert agent_access_profiles.COINBASE_POLICY in haku_policy_sets
    assert (
        agent_access_profiles.COINBASE_POLICY
        in _by_name(staging_manifests, "PolicyBinding", "claude-ai")["spec"]["policySets"]
    )
    assert (
        agent_access_profiles.COINBASE_POLICY
        in _by_name(staging_manifests, "PolicyBinding", "haku-agent")["spec"]["policySets"]
    )
    assert not any(
        doc["kind"] == "PolicySet" and doc["metadata"]["name"] == "coinbase-credentials" for doc in staging_manifests
    )
    coinbase_rules = _by_name(staging_manifests, "PolicySet", agent_access_profiles.COINBASE_POLICY)["spec"]["rules"]
    assert {rule["type"] for rule in coinbase_rules} == {"egress", "kubernetes_role_binding"}
    assert {
        (rule["role"]["kind"], rule["role"]["name"], tuple(rule["namespaces"]))
        for rule in coinbase_rules
        if rule["type"] == "kubernetes_role_binding"
    } == {("Role", "claude-ai-coinbase-reader", ("agentplane-staging",))}
    egress_rule = one(rule for rule in coinbase_rules if rule["type"] == "egress")
    assert egress_rule["hosts"] == ["api.coinbase.com"]
    assert egress_rule["methods"] == ["GET"]
    assert "paths" not in egress_rule
    assert "credentialRef" not in egress_rule
    assert agent_access_profiles.PLAID_DIAGNOSTICS_POLICY in haku_policy_sets
    assert (
        agent_access_profiles.PLAID_DIAGNOSTICS_POLICY
        in _by_name(staging_manifests, "PolicyBinding", "claude-ai")["spec"]["policySets"]
    )
    assert (
        agent_access_profiles.PLAID_DIAGNOSTICS_POLICY
        in _by_name(staging_manifests, "PolicyBinding", "haku-agent")["spec"]["policySets"]
    )
    assert not any(
        doc["kind"] == "PolicySet" and doc["metadata"]["name"] == PLAID_PGWEB_CREDENTIAL for doc in staging_manifests
    )
    assert (
        set(haku) - set(selected)
        == {"haku-sandbox-write", "coinbase-credentials", agent_access_profiles.PLAID_DIAGNOSTICS_POLICY}
        - credential_grants
    )
    assert set(selected) - set(haku) == {"public-coder-node-read", "public-coder-cluster-metadata-read"} | (
        {"spend-policy-config"} if preset == "finance-agent" else set()
    )
    assert {
        "agentplane-staging-metadata",
        "agentplane-staging-logs",
        "haku-console-metadata",
        "clickhouse-diagnostics",
        "ducktape-flux-read",
        "public-coder-volsync-status",
        "public-coder-agent-reader",
    } <= set(selected)
    # Cluster-wide reads are explicit inventory and diagnostics grants. Namespaced
    # log and metadata readers remain namespaced.
    assert {name: catalog[name] for name in selected if catalog[name]["kind"] == "ClusterRoleBinding"} == {
        "public-coder-node-read": {
            "kind": "ClusterRoleBinding",
            "role_ref": {"kind": "ClusterRole", "name": "public-coder-agent-node-reader"},
        },
        "public-coder-cluster-metadata-read": {
            "kind": "ClusterRoleBinding",
            "role_ref": {"kind": "ClusterRole", "name": "public-coder-agent-cluster-metadata-reader"},
        },
        "cluster-diagnostics": {
            "kind": "ClusterRoleBinding",
            "role_ref": {"kind": "ClusterRole", "name": "cluster-diagnostics-reader"},
        },
    }
    assert {
        catalog[name]["role_ref"]["name"]
        for name in selected
        if catalog[name]["kind"] == "RoleBinding" and catalog[name]["role_ref"]["kind"] == "ClusterRole"
    } == {"agent-readable-namespace-metadata", "agent-readable-namespace-logs"}
    assert {
        (catalog[name]["namespace"], catalog[name]["role_ref"]["name"])
        for name in selected
        if catalog[name]["role_ref"]["kind"] == "Role"
    } == {
        ("haku-console", "agent-haku-console-metadata-reader"),
        ("clickhouse", "agent-clickhouse-diagnostics-reader"),
        ("ducktape-flux", "ducktape-flux-reader"),
        ("public-coder-agent", "agent-public-coder-extended-diagnostics-reader"),
        ("public-coder-agent", "public-coder-agent-reader"),
    } | (
        {
            ("agentplane-staging", "claude-ai-coinbase-reader"),
            ("plaid-mcp", "plaid-spend-finance-config-reader"),
            ("plaid-mcp", "plaid-finance-diagnostics-reader"),
        }
        if preset == "finance-agent"
        else set()
    )


@pytest.mark.parametrize(
    "target_namespace", binding_delegation.external_scopes(staging.ENV, staging.assignable_grants())
)
def test_external_delegation_binds_exactly_the_granted_roles(
    scratch_app: App, target_namespace: str, catalog_manifests: list[dict[str, Any]]
) -> None:
    assignable_grants = [grant.model_dump(mode="json") for grant in staging.assignable_grants()]
    granted = {
        grant["role_ref"]["name"]
        for grant in assignable_grants
        if grant["kind"] == "RoleBinding"
        and grant["namespace"] == target_namespace
        and grant["role_ref"]["kind"] == "Role"
    }
    delegated = Cdk8sTesting.synth(
        binding_delegation.chart(scratch_app, staging.ENV, target_namespace, staging.assignable_grants())
    )
    role = one(doc for doc in delegated if doc["kind"] == "Role")
    assert {name for rule in role["rules"] if rule["verbs"] == ["bind"] for name in rule["resourceNames"]} == granted
    # An absent target namespace must not fail the app Kustomization's apply.
    assert not any(
        doc["kind"] in {"Role", "RoleBinding"}
        and doc["metadata"].get("namespace") == target_namespace
        and doc["metadata"]["name"] == role["metadata"]["name"]
        for doc in catalog_manifests
    )


def test_inference_is_granted_separately_from_platform_operations(catalog_manifests: list[dict[str, Any]]) -> None:
    granted = _by_name(catalog_manifests, "PolicySet", INFERENCE_EXPERIMENTS_POLICY)
    basic = _by_name(catalog_manifests, "PolicySet", BASIC_POLICY)
    assert INFERENCE_EXPERIMENTS_POLICY in _environment_default_policies(catalog_manifests)
    # Standing caller identities and static OpenClaw must receive the separate grant too.
    for binding in (doc for doc in catalog_manifests if doc["kind"] == "PolicyBinding"):
        policies = binding["spec"]["policySets"]
        if BASIC_POLICY in policies or binding["metadata"]["name"] == "public-coder-openclaw":
            assert INFERENCE_EXPERIMENTS_POLICY in policies
    inference_hosts = {host for rule in granted["spec"]["rules"] for host in rule["hosts"]}
    assert inference_hosts.isdisjoint(host for rule in basic["spec"]["rules"] for host in rule.get("hosts", []))
    for name, denied_paths in (
        ("ollama", {"/api/pull", "/api/push", "/api/create", "/api/delete", "/api/copy", "/api/blobs/*"}),
        ("litellm-cheap-experiments", {"/key/generate", "/key/info", "/user/new", "/config/update"}),
    ):
        rules = [rule for rule in granted["spec"]["rules"] if rule.get("credentialRef") == {"name": name}]
        assert rules
        assert all(rule["clusterInternal"] and rule["paths"] for rule in rules)
        assert all("*" not in path for rule in rules for path in rule["paths"])
        assert denied_paths.isdisjoint(path for rule in rules for path in rule["paths"])
        assert any("POST" in rule["methods"] and "/v1/chat/completions" in rule["paths"] for rule in rules)
        assert all(set(rule["methods"]) <= {"GET", "POST"} for rule in rules)
        credential = _by_name(catalog_manifests, "EgressCredential", name)
        source = credential["spec"]["source"]["secretRef"]
        secret = _by_name(catalog_manifests, "ExternalSecret", source["name"])
        assert secret["metadata"]["namespace"] == f"{staging.ENV.namespace}-egress-credentials"
        assert source["key"] in {item["secretKey"] for item in secret["spec"]["data"]}


def test_the_proxy_impersonates_only_serviceaccounts_in_the_accounts_namespace(
    catalog_manifests: list[dict[str, Any]],
) -> None:
    proxy = ("ServiceAccount", "egress", staging.ENV.namespace)
    granted = [
        (doc["roleRef"]["kind"], doc["roleRef"]["name"], doc["metadata"].get("namespace"))
        for doc in catalog_manifests
        if doc["kind"] in {"RoleBinding", "ClusterRoleBinding"}
        and any((s["kind"], s["name"], s.get("namespace")) == proxy for s in doc.get("subjects", []))
    ]
    roles = {
        (doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace")): doc
        for doc in catalog_manifests
        if doc["kind"] in {"Role", "ClusterRole"}
    }
    # A RoleBinding to a ClusterRole grants it in the binding's namespace; either way the rules are the role's.
    impersonating = [
        (kind, name, namespace)
        for kind, name, namespace in granted
        if any(
            {"impersonate", "*"} & set(rule["verbs"])
            for rule in roles[(kind, name, None if kind == "ClusterRole" else namespace)]["rules"]
        )
    ]
    kind, name, namespace = one(impersonating)
    assert (kind, namespace) == ("Role", kubernetes_accounts_namespace(staging.ENV.namespace))
    assert roles[(kind, name, namespace)]["rules"] == [
        {"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["impersonate"]}
    ]


def test_the_local_api_server_bearer_is_the_proxys_mounted_token(svid_egress_manifests: list[dict[str, Any]]) -> None:
    settings = yaml.safe_load(
        _by_name(svid_egress_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"]
    )
    server = settings["local_api_server"]
    assert (server["host"], server["port"]) == (KUBERNETES_HOST, 443)
    pod = _by_name(svid_egress_manifests, "Deployment", "egress")["spec"]["template"]["spec"]
    # The kubelet mounts the API-server token here when the Pod automounts it.
    assert pod["automountServiceAccountToken"] is True
    assert PurePosixPath(server["token_file"]).parent == PurePosixPath("/var/run/secrets/kubernetes.io/serviceaccount")


if __name__ == "__main__":
    pytest_bazel.main()


def test_aiquota_read_is_read_only_and_not_public(catalog_manifests: list[dict[str, Any]]) -> None:
    config = yaml.safe_load(_by_name(catalog_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    for name in ("finance-agent", "haku"):
        assert AIQUOTA_READ_POLICY in config["sandbox_presets"][name]["policy_sets"]
    assert AIQUOTA_READ_POLICY not in config["sandbox_presets"]["public-coder"]["policy_sets"]
    quota = _by_name(catalog_manifests, "PolicySet", AIQUOTA_READ_POLICY)
    assert {
        "type": "egress",
        "hosts": [client.HTTP.fqdn],
        "clusterInternal": True,
        "methods": ["GET"],
        "paths": ["/"],
        "credentialRef": {"name": client.AIQUOTA_HISTORY_CREDENTIALS},
    } in quota["spec"]["rules"]
    credential = _by_name(catalog_manifests, "EgressCredential", client.AIQUOTA_HISTORY_CREDENTIALS)
    assert credential["spec"]["source"] == {
        "secretRef": {"name": client.AIQUOTA_HISTORY_CREDENTIALS, "key": client.PASSWORD_KEY}
    }
    assert credential["spec"]["targets"] == [{"header": "Authorization", "method": "basicPassword"}]
    assert any(
        rule.get("clusterInternal")
        and rule.get("hosts") == ["aiquota-api.cli-proxy-api.svc.cluster.local"]
        and rule["methods"] == ["GET"]
        and rule["paths"] == ["/v1/quotas", "/v1/providers/*/raw"]
        for rule in quota["spec"]["rules"]
    )
