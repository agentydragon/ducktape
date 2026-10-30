"""What the namespace's egress grants a sandbox: the Actions API's agent-facing surface only, and
the Kubernetes access every sandbox gets rather than the ones that opted in."""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Testing as Cdk8sTesting
from more_itertools import one

from cluster.cdk8s import agent_access_profiles
from cluster.cdk8s.agentplane import binding_delegation, notifications, staging, staging_grants
from cluster.cdk8s.agentplane.egress import (
    ADMIN_GRPC_AUDIENCE,
    BASIC_POLICY,
    DEFAULT_POLICIES,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    INFERENCE_EXPERIMENTS_POLICY,
    KUBERNETES_AUDIENCE,
    KUBERNETES_CREDENTIAL,
    KUBERNETES_HOST,
    PUBLIC_INTERNET_POLICY,
    agent_api,
)
from cluster.cdk8s.agentplane.egress_staging_credentials import AIQUOTA_READ_POLICY, PLAID_PGWEB_CREDENTIAL
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.model_selections import STAGING_APP_MODELS
from model_catalog.catalog import OLLAMA_QWEN_IQ4XS_256K


def _by_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """The one object of `kind` named `name`, which every test here asserts exists."""
    return one(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _app_default_policies(docs: list[dict[str, Any]]) -> list[str]:
    """The PolicySets the app grants to each Sandbox it creates."""
    config = _by_name(docs, "ConfigMap", "app-config")
    defaults: list[str] = yaml.safe_load(config["data"]["config.yaml"])["default_policy_sets"]
    return defaults


def test_egress_uses_go_proxy_and_goose_migration(staging_manifests: list[dict[str, Any]]) -> None:
    pod = _by_name(staging_manifests, "Deployment", "egress")["spec"]["template"]["spec"]
    proxy = one(container for container in pod["containers"] if container["name"] == "proxy")
    migrator = one(container for container in pod["initContainers"] if container["name"] == "migrate")
    assert proxy["image"] == "git.allegedly.works/ducktape-ci/agentplane-egress-proxy:unset"
    assert migrator["image"] == "git.allegedly.works/ducktape-ci/agentplane-egress-history-migrate:unset"
    proxy_env = {entry["name"]: entry for entry in proxy["env"]}
    migrator_env = {entry["name"]: entry for entry in migrator["env"]}
    assert migrator_env["DATABASE_URL"]["valueFrom"] == proxy_env["AGENTPLANE_EGRESS_DATABASE_URL"]["valueFrom"]
    assert "PGSSLMODE" not in proxy_env
    assert "PGSSLMODE" not in migrator_env
    assert any(volume.get("configMap", {}).get("name") == "egress-go-settings" for volume in pod["volumes"])
    assert "args" not in proxy
    assert proxy_env["AGENTPLANE_EGRESS_RULES_NAMESPACE"]["value"] == staging.ENV.namespace
    config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"])
    assert config["admin_grpc_audience"] == ADMIN_GRPC_AUDIENCE
    assert config["admin_grpc_callers"] == [f"{staging.ENV.namespace}/app"]
    assert not {"confdir", "decision_flush_seconds"} & config.keys()


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


def test_app_default_policy_sets_are_canonical(staging_manifests: list[dict[str, Any]]) -> None:
    app_defaults = _app_default_policies(staging_manifests)
    assert app_defaults == sorted(DEFAULT_POLICIES)
    assert PUBLIC_INTERNET_POLICY not in app_defaults


def test_public_internet_is_available_but_never_implicitly_granted(staging_manifests: list[dict[str, Any]]) -> None:
    public = _by_name(staging_manifests, "PolicySet", PUBLIC_INTERNET_POLICY)
    assert public["spec"]["rules"] == [{"type": "egress", "hosts": ["*"]}]
    # Every preset, not just today's public-coder preset, and the defaults.
    assert PUBLIC_INTERNET_POLICY not in _by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"]
    assert PUBLIC_INTERNET_POLICY not in _app_default_policies(staging_manifests)
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


def test_agent_egress_api_uses_its_canonical_service(staging_manifests: list[dict[str, Any]]) -> None:
    policy = one(
        doc for doc in staging_manifests if doc["kind"] == "PolicySet" and doc["metadata"]["name"] == BASIC_POLICY
    )
    canonical_host = agent_api(staging.ENV.namespace).fqdn
    rule = one(rule for rule in policy["spec"]["rules"] if canonical_host in rule["hosts"])
    assert rule["hosts"] == [canonical_host]
    assert rule["methods"] == ["GET"]
    assert rule["paths"] == ["/llms.txt", "/docs/agents/**", "/openapi.json", "/v1/rules"]


def test_kubernetes_access_is_part_of_what_every_sandbox_is_granted(staging_manifests: list[dict[str, Any]]) -> None:
    """Every agent talks to the API server, so the rule admitting it is in `basic`.

    `basic` is the policy every launch is granted before the caller picks anything
    (`SandboxAccess`, agentplane/app/sandbox_access.py), which is what makes
    Kubernetes access a property of a sandbox rather than a choice one. It was its own policy
    while every SandboxTemplate already mounted the kubeconfig naming this credential
    (sandbox_pod.py): a box whose preset or caller did not name it held a working-looking config
    whose requests the proxy refused for want of a rule, a transport failure dressed up as an
    authorization answer.

    What makes it legitimate as an unconditional grant is what gets substituted. A default is the
    one grant a caller cannot decline, so it may only ever substitute the sandbox's own identity;
    this credential is that Pod's ServiceAccount projected for the API server's audience, and a
    `secretRef` source here would hand an operator's credential to every sandbox in the namespace.
    What a box may then do is the RBAC bound to its account, the only axis this narrows -- which
    is why the rule names neither methods nor paths.
    """
    basic = _by_name(staging_manifests, "PolicySet", BASIC_POLICY)
    api_server = one(rule for rule in basic["spec"]["rules"] if KUBERNETES_HOST in rule["hosts"])
    assert api_server.get("credentialRef") == {"name": KUBERNETES_CREDENTIAL}, api_server
    # Narrowing by verb or path would be a second, weaker copy of RBAC; RBAC decides.
    assert "methods" not in api_server, api_server
    assert "paths" not in api_server, api_server

    credential = _by_name(staging_manifests, "EgressCredential", KUBERNETES_CREDENTIAL)
    source = credential["spec"]["source"]
    assert "secretRef" not in source, "a default may substitute a sandbox's own credential, never an operator's"
    assert source["projectedWorkloadToken"]["audience"] == KUBERNETES_AUDIENCE, source

    # And `basic` reaches a sandbox that picks nothing, which is the whole claim.
    defaults = _app_default_policies(staging_manifests)
    assert BASIC_POLICY in defaults, defaults


def test_notifications_use_their_own_projected_audience(staging_manifests: list[dict[str, Any]]) -> None:
    policy = _by_name(staging_manifests, "PolicySet", BASIC_POLICY)
    notification_rule = one(
        rule for rule in policy["spec"]["rules"] if notifications.service(staging.ENV.namespace).fqdn in rule["hosts"]
    )
    assert notification_rule["credentialRef"] == {"name": notifications.WORKLOAD_CREDENTIAL}
    credential = _by_name(staging_manifests, "EgressCredential", notifications.WORKLOAD_CREDENTIAL)
    assert credential["spec"]["source"]["projectedWorkloadToken"]["audience"] == notifications.TOKEN_AUDIENCE
    settings = _by_name(staging_manifests, "ConfigMap", "egress-go-settings")["data"]["settings.yaml"]
    proxy_settings = yaml.safe_load(settings)
    assert notifications.TOKEN_AUDIENCE in proxy_settings["projected_token_audiences"]
    notification_settings = yaml.safe_load(
        _by_name(staging_manifests, "ConfigMap", f"{notifications.NAME}-settings")["data"]["settings.yaml"]
    )
    assert notification_settings["token_audience"] == notifications.TOKEN_AUDIENCE


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
    assert all(route.id in config["models"]["harnesses"]["HARNESS_CODEX"] for route in STAGING_APP_MODELS.claude)
    options = {model["model"]: model for model in config["models"]["models"]}
    assert all(
        options[route.id]["display_name"].endswith(" — Google Antigravity") for route in STAGING_APP_MODELS.claude
    )
    assert all(
        options[route.id]["reasoning_efforts"] == ["low", "medium", "high"] for route in STAGING_APP_MODELS.claude
    )
    assert all(preset["harness"] == "HARNESS_CODEX" for preset in config["thread_presets"].values())
    # The Anthropic Messages endpoint remains available to Claude sessions.
    assert any(doc["kind"] == "Service" and doc["metadata"]["name"] == "llm-ingress" for doc in staging_manifests)


def test_central_sampler_offers_the_codex_models(staging_manifests: list[dict[str, Any]]) -> None:
    config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    catalog = config["models"]
    assert catalog["harnesses"]["HARNESS_CENTRAL_SAMPLER"] == catalog["harnesses"]["HARNESS_CODEX"]
    assert catalog["central_sampler"] == {"service": "central-sampler"}
    sampler = json.loads(_by_name(staging_manifests, "ConfigMap", "central-sampler-settings")["data"]["settings.json"])
    assert set(sampler["models"]) == {route.id for route in STAGING_APP_MODELS.codex}


def test_haku_launches_on_codex_with_the_wider_qwen_context_window(staging_manifests: list[dict[str, Any]]) -> None:
    """Haku's preset remains on the local Qwen route until its Claude path is validated.

    The preset is the only reason a launch form picks a model, so it must name a route the
    harness accepts and an effort that route declares.
    """
    config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    preset = config["thread_presets"]["haku-codex"]
    assert preset["harness"] == "HARNESS_CODEX"
    assert preset["model"] == OLLAMA_QWEN_IQ4XS_256K.openai.id
    assert preset["model"] in config["models"]["harnesses"]["HARNESS_CODEX"]
    option = one(model for model in config["models"]["models"] if model["model"] == preset["model"])
    assert preset["reasoning_effort"] in option["reasoning_efforts"]
    assert config["sandbox_presets"]["haku"]["thread_preset"] == "haku-codex"


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
    assert INFERENCE_EXPERIMENTS_POLICY in _app_default_policies(staging_manifests)
    # Standing caller identities and static OpenClaw must receive the separate grant too.
    for binding in (doc for doc in staging_manifests if doc["kind"] == "PolicyBinding"):
        policies = binding["spec"]["policySets"]
        if BASIC_POLICY in policies or binding["metadata"]["name"] == "public-coder-openclaw":
            assert INFERENCE_EXPERIMENTS_POLICY in policies
    inference_hosts = {host for rule in granted["spec"]["rules"] for host in rule["hosts"]}
    assert inference_hosts.isdisjoint(host for rule in basic["spec"]["rules"] for host in rule["hosts"])
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
