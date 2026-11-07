"""What the namespace's egress grants a sandbox: the Actions API's agent-facing surface only, and
the Kubernetes access every sandbox gets rather than the ones that opted in."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.egress.kubernetes_audience import KUBERNETES_AUDIENCE
from agentplane.egress.settings import Settings as EgressSettings
from cluster.cdk8s import agent_access_profiles
from cluster.cdk8s.agentplane import binding_delegation, notifications, staging, staging_grants
from cluster.cdk8s.agentplane.egress import (
    ADMIN_GRPC_AUDIENCE,
    BASIC_POLICY,
    DEFAULT_POLICIES,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    INFERENCE_EXPERIMENTS_POLICY,
    KUBERNETES_CREDENTIAL,
    KUBERNETES_HOST,
    PUBLIC_INTERNET_POLICY,
)
from cluster.cdk8s.agentplane.egress_staging_credentials import AIQUOTA_READ_POLICY, PLAID_PGWEB_CREDENTIAL
from cluster.cdk8s.agentplane.model_selections import STAGING_APP_MODELS
from cluster.cdk8s.clickhouse import client
from model_catalog.catalog import Provider


def _by_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """The one object of `kind` named `name`, which every test here asserts exists."""
    return one(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _environment_default_policies(docs: list[dict[str, Any]]) -> list[str]:
    """The PolicySets EnvironmentService applies to each new Sandbox."""
    config = _by_name(docs, "ConfigMap", "runner-gateway-settings")
    defaults: list[str] = yaml.safe_load(config["data"]["settings.yaml"])["environment_default_policy_sets"]
    return defaults


def test_egress_uses_embedded_migrations_and_startup_probe(staging_manifests: list[dict[str, Any]]) -> None:
    pod = _by_name(staging_manifests, "Deployment", "egress")["spec"]["template"]["spec"]
    proxy = one(container for container in pod["containers"] if container["name"] == "proxy")
    assert not pod.get("initContainers")
    assert proxy["startupProbe"]["grpc"] == {"port": proxy["readinessProbe"]["grpc"]["port"], "service": "livez"}
    proxy_env = {entry["name"]: entry for entry in proxy["env"]}
    assert "PGSSLMODE" not in proxy_env
    settings_path = PurePosixPath(proxy_env[EgressSettings.config_file_env]["value"])
    settings_mount = one(mount for mount in proxy["volumeMounts"] if mount["mountPath"] == str(settings_path.parent))
    settings_volume = one(volume for volume in pod["volumes"] if volume["name"] == settings_mount["name"])
    assert settings_volume["configMap"]["name"] == "egress-go-settings"
    assert settings_path.name in _by_name(staging_manifests, "ConfigMap", "egress-go-settings")["data"]
    assert "args" not in proxy
    assert proxy_env["AGENTPLANE_EGRESS_RULES_NAMESPACE"]["value"] == staging.ENV.namespace
    config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"])
    assert config["admin_grpc_audience"] == ADMIN_GRPC_AUDIENCE
    assert config["admin_grpc_callers"] == [f"{staging.ENV.namespace}/app"]


def test_app_projects_egress_admin_audience(staging_manifests: list[dict[str, Any]]) -> None:
    pod = _by_name(staging_manifests, "Deployment", "app")["spec"]["template"]["spec"]
    container = one(pod["containers"])
    volume = one(volume for volume in pod["volumes"] if volume["name"] == "egress-admin-token")
    (token,) = volume["projected"]["sources"]
    assert token["serviceAccountToken"]["audience"] == ADMIN_GRPC_AUDIENCE
    assert token["serviceAccountToken"]["path"] == "token"
    assert {mount["name"]: mount["mountPath"] for mount in container["volumeMounts"]}[
        "egress-admin-token"
    ] == "/var/run/secrets/agentplane-egress-admin"


def test_environment_default_policy_sets_are_canonical(staging_manifests: list[dict[str, Any]]) -> None:
    app_defaults = _environment_default_policies(staging_manifests)
    assert app_defaults == sorted(DEFAULT_POLICIES)
    assert PUBLIC_INTERNET_POLICY not in app_defaults


def test_basic_policy_auto_approves_only_same_principal_session_actions(
    staging_manifests: list[dict[str, Any]],
) -> None:
    basic = _by_name(staging_manifests, "PolicySet", BASIC_POLICY)
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
    assert BASIC_POLICY in _environment_default_policies(staging_manifests)
    for account in ("claude-ai", "haku-agent"):
        binding = _by_name(staging_manifests, "PolicyBinding", account)
        assert BASIC_POLICY in binding["spec"]["policySets"]
    app_config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert app_config["sandbox_presets"]
    for preset in app_config["sandbox_presets"].values():
        assert BASIC_POLICY in preset["policy_sets"]


def test_public_internet_is_available_but_never_implicitly_granted(staging_manifests: list[dict[str, Any]]) -> None:
    public = _by_name(staging_manifests, "PolicySet", PUBLIC_INTERNET_POLICY)
    assert public["spec"]["rules"] == [{"type": "egress", "hosts": ["*"]}]
    # Every preset, not just today's public-coder preset, and the defaults.
    assert PUBLIC_INTERNET_POLICY not in _by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"]
    assert PUBLIC_INTERNET_POLICY not in _environment_default_policies(staging_manifests)
    for doc in staging_manifests:
        if doc["kind"] == "PolicyBinding":
            if doc["metadata"]["name"] == "public-coder-openclaw":
                assert doc["spec"]["subject"] == {"namespace": "public-coder-agent", "name": "openclaw"}
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


def test_sandboxes_no_longer_have_an_http_egress_credential_for_notifications(
    staging_manifests: list[dict[str, Any]],
) -> None:
    policy = _by_name(staging_manifests, "PolicySet", BASIC_POLICY)
    notification_host = notifications.service(staging.ENV.namespace).fqdn
    assert all(notification_host not in rule.get("hosts", []) for rule in policy["spec"]["rules"])
    assert all(
        manifest.get("metadata", {}).get("name") != "agentplane-notifications-workload"
        for manifest in staging_manifests
        if manifest.get("kind") == "EgressCredential"
    )
    settings = _by_name(staging_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"]
    # The direct sidecar gRPC route projects its own token and bypasses the central proxy.
    assert notifications.TOKEN_AUDIENCE not in yaml.safe_load(settings)["projected_token_audiences"]
    notification_settings = yaml.safe_load(
        _by_name(staging_manifests, "ConfigMap", f"{notifications.NAME}-settings")["data"]["settings.yaml"]
    )
    assert notification_settings["token_audience"] == notifications.TOKEN_AUDIENCE


def test_session_dispatcher_egress_is_limited_to_the_delegation_rpc_surfaces(
    staging_manifests: list[dict[str, Any]],
) -> None:
    """Only the Session and inventory RPCs get the Sandbox-bound bearer substitution.

    The Gateway listener also hosts unrelated Runner-control RPCs. Compare normalized permissions
    for rules that can reach this Service or consume its credential, so rule grouping and ordering
    are irrelevant while a new route, method, destination, or uncredentialed path still fails.
    """
    destination = "session-dispatcher.agentplane-staging.svc.cluster.local"
    service_url = f"http://{destination}:8080"
    credential_name = "agentplane-session-dispatcher-workload"
    audience = "session-dispatcher"
    credential_placeholder = f"agentplane-credential-{credential_name}"
    session_paths = [
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/CreateSession",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/GetSession",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ListSessions",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/GetDispatch",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/CancelDispatch",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/InterruptTurn",
    ]
    environment_service_paths = [
        "/agentplane.environments.v1.EnvironmentService/ListEnvironments",
        "/agentplane.environments.v1.EnvironmentService/GetEnvironment",
        "/agentplane.environments.v1.EnvironmentService/WatchEnvironment",
        "/agentplane.environments.v1.EnvironmentService/ResumeEnvironment",
    ]
    expected_permissions = {
        (BASIC_POLICY, credential_name, destination, "POST", path, True)
        for path in (*session_paths, *environment_service_paths)
    }

    def host_matches_destination(pattern: str) -> bool:
        return pattern in {destination, "*"} or (
            pattern.startswith("*.") and destination.endswith(pattern[1:]) and destination != pattern[2:]
        )

    actual_permissions: set[tuple[str, str | None, str, str, str, bool]] = set()
    for policy in staging_manifests:
        if policy["kind"] != "PolicySet":
            continue
        for rule in policy["spec"]["rules"]:
            credential_ref = rule.get("credentialRef", {}).get("name")
            # This separate opt-in set intentionally grants uncredentialed public access to any host.
            if policy["metadata"]["name"] == PUBLIC_INTERNET_POLICY and credential_ref is None:
                continue
            if not any(host_matches_destination(host) for host in rule.get("hosts", [])) and (
                credential_ref != credential_name
            ):
                continue

            hosts = rule.get("hosts")
            methods = rule.get("methods")
            paths = rule.get("paths")
            assert hosts, rule
            assert methods, rule
            assert paths, rule
            actual_permissions.update(
                (policy["metadata"]["name"], credential_ref, host, method, path, rule.get("clusterInternal", False))
                for host in hosts
                for method in methods
                for path in paths
            )

    assert actual_permissions == expected_permissions

    credential = _by_name(staging_manifests, "EgressCredential", credential_name)
    gateway = _by_name(staging_manifests, "Deployment", "runner-gateway")["spec"]["template"]["spec"]
    gateway_container = one(container for container in gateway["containers"] if container["name"] == "runner-gateway")
    gateway_env = {entry["name"]: entry["value"] for entry in gateway_container["env"] if "value" in entry}
    dispatcher_audience = gateway_env["AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_AUDIENCE"]
    assert dispatcher_audience == audience
    assert credential["spec"]["source"] == {"projectedWorkloadToken": {"audience": dispatcher_audience}}
    assert credential["spec"]["targets"] == [{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}]
    proxy_settings = yaml.safe_load(
        _by_name(staging_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"]
    )
    assert proxy_settings["projected_token_audiences"].count(dispatcher_audience) == 1

    # Both execution environments pass only the inert placeholder to the agent. The VM carries
    # that value in its guest runner config; the Pod carries it in its runner container env.
    pod_template = _by_name(staging_manifests, "SandboxTemplate", "environment-tools")
    pod_runner = one(
        container
        for container in pod_template["spec"]["pod"]["spec"]["containers"]
        if container["name"] == "environment-runtime"
    )
    pod_env = {entry["name"]: entry["value"] for entry in pod_runner["env"] if "value" in entry}
    assert pod_env["AGENTPLANE_SESSIONS_URL"] == service_url
    assert pod_env["AGENTPLANE_SESSIONS_CREDENTIAL"] == credential_placeholder

    vm_runner = json.loads(
        _by_name(staging_manifests, "ConfigMap", "environment-vm-config")["data"]["environment-runtime.json"]
    )
    assert vm_runner["harness_environment"]["values"]["AGENTPLANE_SESSIONS_URL"] == service_url
    assert vm_runner["harness_environment"]["values"]["AGENTPLANE_SESSIONS_CREDENTIAL"] == credential_placeholder


def test_github_policy_admits_the_write_verbs_a_pr_lifecycle_needs(staging_manifests: list[dict[str, Any]]) -> None:
    """The bot can finish a pull request, not only open one.

    GitHub answers a PR description with PATCH and `update-branch`/`merge` with PUT; a review comment
    or a merged head branch sits behind DELETE. POST alone leaves a sandbox able to file a change and
    unable to correct it. Pinned in both directions so that trimming a verb stays a decision rather
    than a side effect of tidying, and so the rule stays unpathed rather than narrowing quietly.
    """
    github = _by_name(staging_manifests, "PolicySet", GITHUB_AGENTYDRAGON_AGENT_POLICY)
    rule = one(github["spec"]["rules"])
    assert set(rule["methods"]) == {"GET", "POST", "PUT", "PATCH", "DELETE"}
    assert rule["credentialRef"] == {"name": "github-pat"}
    # Unpathed: the verb list bounds the method, while the PAT behind it stays whole (egress.py).
    assert "paths" not in rule


def test_claude_picker_offers_environment_models_and_keeps_ingress(staging_manifests: list[dict[str, Any]]) -> None:
    config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert config["models"]["harnesses"]["HARNESS_CLAUDE"] == [route.id for route in STAGING_APP_MODELS.claude]
    assert all(
        route.id in config["models"]["harnesses"]["HARNESS_CENTRAL_SAMPLER"] for route in STAGING_APP_MODELS.claude
    )
    for preset in config["thread_presets"].values():
        assert preset["model"] in config["models"]["harnesses"][preset["harness"]]
    # The Anthropic Messages endpoint remains available to Claude sessions.
    assert any(doc["kind"] == "Service" and doc["metadata"]["name"] == "llm-ingress" for doc in staging_manifests)


def test_shared_coding_models_preserve_provider_protocols(staging_manifests: list[dict[str, Any]]) -> None:
    config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    catalog = config["models"]
    assert catalog["harnesses"]["HARNESS_CODEX"] == [route.id for route in STAGING_APP_MODELS.codex]
    assert catalog["harnesses"]["HARNESS_CENTRAL_SAMPLER"] == [route.id for route in STAGING_APP_MODELS.central_sampler]
    assert set(catalog["harnesses"]["HARNESS_CODEX"]) < set(catalog["harnesses"]["HARNESS_CENTRAL_SAMPLER"])
    assert "central_sampler" not in catalog
    sampler = json.loads(_by_name(staging_manifests, "ConfigMap", "central-sampler-settings")["data"]["settings.json"])
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


def test_haku_launches_on_an_available_central_sampler_route(staging_manifests: list[dict[str, Any]]) -> None:
    """Haku's model and effort must be accepted by the selected harness.

    Sandbox and Action launches use the same preset, so both must resolve to that harness and model.
    """
    config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    preset = config["thread_presets"]["haku-codex"]
    assert preset["harness"] == "HARNESS_CENTRAL_SAMPLER"
    assert preset["model"] in config["models"]["harnesses"]["HARNESS_CENTRAL_SAMPLER"]
    option = one(model for model in config["models"]["models"] if model["model"] == preset["model"])
    assert preset["reasoning_effort"] in option["reasoning_efforts"]
    assert config["sandbox_presets"]["haku"]["thread_preset"] == "haku-codex"
    actions = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "actions-settings")["data"]["settings.yaml"])
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
    scratch_app: App, target_namespace: str, staging_manifests: list[dict[str, Any]]
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
        for doc in staging_manifests
    )


def test_inference_is_granted_separately_from_platform_operations(staging_manifests: list[dict[str, Any]]) -> None:
    granted = _by_name(staging_manifests, "PolicySet", INFERENCE_EXPERIMENTS_POLICY)
    basic = _by_name(staging_manifests, "PolicySet", BASIC_POLICY)
    assert INFERENCE_EXPERIMENTS_POLICY in _environment_default_policies(staging_manifests)
    # Standing caller identities and static OpenClaw must receive the separate grant too.
    for binding in (doc for doc in staging_manifests if doc["kind"] == "PolicyBinding"):
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
        credential = _by_name(staging_manifests, "EgressCredential", name)
        source = credential["spec"]["source"]["secretRef"]
        secret = _by_name(staging_manifests, "ExternalSecret", source["name"])
        assert secret["metadata"]["namespace"] == f"{staging.ENV.namespace}-egress-credentials"
        assert source["key"] in {item["secretKey"] for item in secret["spec"]["data"]}


if __name__ == "__main__":
    pytest_bazel.main()


def test_aiquota_read_is_read_only_and_not_public(staging_manifests: list[dict[str, Any]]) -> None:
    config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    for name in ("finance-agent", "haku"):
        assert AIQUOTA_READ_POLICY in config["sandbox_presets"][name]["policy_sets"]
    assert AIQUOTA_READ_POLICY not in config["sandbox_presets"]["public-coder"]["policy_sets"]
    quota = _by_name(staging_manifests, "PolicySet", AIQUOTA_READ_POLICY)
    assert {
        "type": "egress",
        "hosts": [client.HTTP.fqdn],
        "clusterInternal": True,
        "methods": ["GET"],
        "paths": ["/"],
        "credentialRef": {"name": client.AIQUOTA_HISTORY_CREDENTIALS},
    } in quota["spec"]["rules"]
    credential = _by_name(staging_manifests, "EgressCredential", client.AIQUOTA_HISTORY_CREDENTIALS)
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
