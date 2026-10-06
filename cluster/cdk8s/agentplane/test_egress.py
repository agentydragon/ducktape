"""What the namespace's egress grants a sandbox: the Actions API's agent-facing surface only, and
the Kubernetes access every sandbox gets rather than the ones that opted in."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting
from more_itertools import one

from cluster.cdk8s.agentplane import binding_delegation, notifications, staging, testing
from cluster.cdk8s.agentplane.app_settings import (
    ACTIVITYWATCH_READ_POLICY,
    AGENTPLANE_TESTING_POLICY,
    AIQUOTA_READ_POLICY,
    BASIC_POLICY,
    COINBASE_POLICY,
    FINANCE_AIQUOTA_HISTORY_POLICY,
    FORGEJO_FINANCE_AGENT_POLICY,
    FORGEJO_HAKU_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GOOGLE_READONLY_POLICY,
    GROCY_SF_READONLY_POLICY,
    HAKU_MAILBOX_POLICY,
    HOME_ASSISTANT_READONLY_POLICY,
    INFERENCE_EXPERIMENTS_POLICY,
    PLAID_PGWEB_POLICY,
    PUBLIC_INTERNET_POLICY,
)
from cluster.cdk8s.agentplane.conftest import NAMESPACES
from cluster.cdk8s.agentplane.egress import KUBERNETES_AUDIENCE, KUBERNETES_CREDENTIAL, KUBERNETES_HOST
from cluster.cdk8s.clickhouse import client
from model_catalog.catalog import OLLAMA_QWEN_IQ4XS_256K

# What a workload token may reach on the Actions service: the MCP endpoint, its schema and
# the action-group/request API. The operator API (/v1/operator/*) and the OAuth endpoints
# (/register, /token, ...) stay off this policy.
_AGENT_FACING_PREFIXES = ("/mcp", "/openapi.json", "/v1/action-")


def _by_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """The one object of `kind` named `name`, which every test here asserts exists."""
    return one(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_public_internet_is_available_but_never_implicitly_granted(
    agentplane_manifests: dict[str, list[dict[str, Any]]], namespace: str
) -> None:
    docs = agentplane_manifests[namespace]
    public = _by_name(docs, "EgressPolicy", PUBLIC_INTERNET_POLICY)
    assert public["spec"]["rules"] == [{"hosts": ["*"]}]
    config = _by_name(docs, "ConfigMap", "agentplane-app-config")
    # Includes defaults and every preset, not just today's public-coder preset.
    assert PUBLIC_INTERNET_POLICY not in config["data"]["config.yaml"]
    for doc in docs:
        if doc["kind"] == "EgressBinding":
            if namespace == "agentplane-staging" and doc["metadata"]["name"] == "public-coder-openclaw":
                assert doc["spec"]["subjects"] == [{"namespace": "public-coder-agent", "name": "openclaw"}]
                assert doc["spec"]["policies"] == [
                    "public-coder-openclaw",
                    INFERENCE_EXPERIMENTS_POLICY,
                    PUBLIC_INTERNET_POLICY,
                ]
            else:
                assert PUBLIC_INTERNET_POLICY not in doc["spec"]["policies"]


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_workload_policy_grants_only_the_agent_facing_actions_api(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    policy = one(
        doc
        for doc in agentplane_manifests[namespace]
        if doc["kind"] == "EgressPolicy" and doc["metadata"]["name"] == BASIC_POLICY
    )
    rule = one(rule for rule in policy["spec"]["rules"] if one(rule["hosts"]).startswith("agentplane-actions."))
    assert rule["paths"], "a rule without paths admits every path on the host"
    assert all(path.startswith(_AGENT_FACING_PREFIXES) for path in rule["paths"])


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_kubernetes_access_is_part_of_what_every_sandbox_is_granted(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    """Every agent talks to the API server, so the rule admitting it is in `basic`.

    `basic` is the policy every launch is granted before the caller picks anything
    (`default_egress_policies` and `launch_policies`, agentplane/app/egress.py), which is what makes
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
    docs = agentplane_manifests[namespace]
    basic = _by_name(docs, "EgressPolicy", BASIC_POLICY)
    api_server = one(rule for rule in basic["spec"]["rules"] if KUBERNETES_HOST in rule["hosts"])
    assert api_server.get("credentialRef") == {"name": KUBERNETES_CREDENTIAL}, api_server
    # Narrowing by verb or path would be a second, weaker copy of RBAC; RBAC decides.
    assert "methods" not in api_server, api_server
    assert "paths" not in api_server, api_server

    credential = _by_name(docs, "EgressCredential", KUBERNETES_CREDENTIAL)
    source = credential["spec"]["source"]
    assert "secretRef" not in source, "a default may substitute a sandbox's own credential, never an operator's"
    assert source["projectedWorkloadToken"]["audience"] == KUBERNETES_AUDIENCE, source

    # And `basic` reaches a sandbox that picks nothing, which is the whole claim.
    app_config = _by_name(docs, "ConfigMap", "agentplane-app-config")
    defaults: list[str] = yaml.safe_load(app_config["data"]["config.yaml"])["default_egress_policies"]
    assert BASIC_POLICY in defaults, defaults


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_notifications_use_their_own_projected_audience(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    docs = agentplane_manifests[namespace]
    policy = _by_name(docs, "EgressPolicy", BASIC_POLICY)
    notification_rule = one(
        rule for rule in policy["spec"]["rules"] if notifications.service(namespace).fqdn in rule["hosts"]
    )
    assert notification_rule["credentialRef"] == {"name": notifications.WORKLOAD_CREDENTIAL}
    credential = _by_name(docs, "EgressCredential", notifications.WORKLOAD_CREDENTIAL)
    assert credential["spec"]["source"]["projectedWorkloadToken"]["audience"] == notifications.TOKEN_AUDIENCE
    settings = _by_name(docs, "ConfigMap", "agentplane-egress-settings")["data"]["settings.yaml"]
    proxy_settings = yaml.safe_load(settings)
    assert notifications.TOKEN_AUDIENCE in proxy_settings["projected_token_audiences"]
    notification_settings = yaml.safe_load(
        _by_name(docs, "ConfigMap", f"{notifications.NAME}-settings")["data"]["settings.yaml"]
    )
    assert notification_settings["token_audience"] == notifications.TOKEN_AUDIENCE


def test_testing_github_policy_has_its_credential_and_no_real_account_credentials(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    manifests = agentplane_manifests[testing.ENV.namespace]
    github = one(
        doc
        for doc in manifests
        if doc["kind"] == "EgressPolicy" and doc["metadata"]["name"] == GITHUB_AGENTYDRAGON_AGENT_POLICY
    )
    github_rule = one(github["spec"]["rules"])
    assert "codeload.github.com" in github_rule["hosts"]
    credential_name = github_rule["credentialRef"]["name"]
    credential = one(
        doc for doc in manifests if doc["kind"] == "EgressCredential" and doc["metadata"]["name"] == credential_name
    )
    assert credential["spec"]["source"]["secretRef"]
    assert not any(
        doc["kind"] in {"EgressCredential", "EgressPolicy"}
        and doc["metadata"]["name"]
        in {
            FORGEJO_HAKU_POLICY,
            FORGEJO_FINANCE_AGENT_POLICY,
            GOOGLE_READONLY_POLICY,
            GROCY_SF_READONLY_POLICY,
            HOME_ASSISTANT_READONLY_POLICY,
            ACTIVITYWATCH_READ_POLICY,
            AGENTPLANE_TESTING_POLICY,
            AIQUOTA_READ_POLICY,
            FINANCE_AIQUOTA_HISTORY_POLICY,
            HAKU_MAILBOX_POLICY,
            PLAID_PGWEB_POLICY,
        }
        for doc in manifests
    )


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_claude_pause_omits_launch_offerings_but_keeps_ingress(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    docs = agentplane_manifests[namespace]
    config = yaml.safe_load(_by_name(docs, "ConfigMap", "agentplane-app-config")["data"]["config.yaml"])
    assert config["models"]["harnesses"]["HARNESS_CLAUDE"] == []
    assert config["models"]["harnesses"]["HARNESS_CODEX"]
    assert all(preset["harness"] == "HARNESS_CODEX" for preset in config["thread_presets"].values())
    # Existing sessions retain the Anthropic ingress endpoint and proxy policy.
    assert any(doc["kind"] == "Service" and doc["metadata"]["name"] == "agentplane-llm-ingress" for doc in docs)


def test_haku_launches_on_codex_with_the_wider_qwen_context_window(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    """Haku's preset follows the paused Claude offerings onto the local Qwen route.

    The preset is the only reason a launch form picks a model, so it must name a route the
    harness accepts and an effort that route declares; agentplane-testing offers no Haku preset
    at all, since only staging provisions Haku's credentials.
    """
    docs = agentplane_manifests[staging.ENV.namespace]
    config = yaml.safe_load(_by_name(docs, "ConfigMap", "agentplane-app-config")["data"]["config.yaml"])
    preset = config["thread_presets"]["haku-codex"]
    assert preset["harness"] == "HARNESS_CODEX"
    assert preset["model"] == OLLAMA_QWEN_IQ4XS_256K.openai.id
    assert preset["model"] in config["models"]["harnesses"]["HARNESS_CODEX"]
    option = one(model for model in config["models"]["models"] if model["model"] == preset["model"])
    assert preset["reasoning_effort"] in option["reasoning_efforts"]
    assert config["sandbox_presets"]["haku"]["thread_preset"] == "haku-codex"

    testing_config = yaml.safe_load(
        _by_name(agentplane_manifests[testing.ENV.namespace], "ConfigMap", "agentplane-app-config")["data"][
            "config.yaml"
        ]
    )
    assert not {"haku", "haku-codex"} & set(testing_config["sandbox_presets"])
    assert not {"haku", "haku-codex"} & set(testing_config["thread_presets"])


@pytest.mark.parametrize("preset", ["public-coder", "finance-agent"])
def test_public_diagnostics_share_haku_reads_but_not_privileged_grants(
    preset: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    docs = agentplane_manifests[staging.ENV.namespace]
    config = yaml.safe_load(_by_name(docs, "ConfigMap", "agentplane-app-config")["data"]["config.yaml"])
    catalog = config["kubernetes_grants"]
    selected = config["sandbox_presets"][preset]["kubernetes_grants"]
    credential_grants = {"coinbase-credentials", "spend-private-config"} if preset == "finance-agent" else set()
    # Coinbase egress follows the Secret grant; haku-agent's EgressBinding carries none.
    assert (COINBASE_POLICY in config["sandbox_presets"][preset]["egress_policies"]) == (preset == "finance-agent")
    assert COINBASE_POLICY not in _by_name(docs, "EgressBinding", "haku-agent")["spec"]["policies"]
    assert set(selected) == set(config["sandbox_presets"]["public-coder"]["kubernetes_grants"]) | credential_grants
    assert len(selected) == len(set(selected))
    haku = config["sandbox_presets"]["haku"]["kubernetes_grants"]
    assert len(haku) == len(set(haku))
    assert (
        set(haku) - set(selected)
        == {"cluster-diagnostics", "haku-sandbox-write", "coinbase-credentials"} - credential_grants
    )
    assert set(selected) - set(haku) == {"public-coder-node-read", "public-coder-cluster-metadata-read"} | (
        {"spend-private-config"} if preset == "finance-agent" else set()
    )
    assert {
        "agentplane-staging-metadata",
        "agentplane-staging-logs",
        "haku-console-metadata",
        "clickhouse-diagnostics",
        "ducktape-flux-read",
        "public-coder-volsync-status",
        "public-coder-agent-reader",
        "agentplane-testing-operator",
        "agentplane-testing-login",
    } <= set(selected)
    # Only these two narrow ClusterRoles may be bound cluster-wide. In particular,
    # never bind the namespaced log/metadata readers or Haku's broader reader there.
    assert {name: catalog[name] for name in selected if catalog[name]["kind"] == "ClusterRoleBinding"} == {
        "public-coder-node-read": {
            "kind": "ClusterRoleBinding",
            "role_ref": {"kind": "ClusterRole", "name": "public-coder-agent-node-reader"},
        },
        "public-coder-cluster-metadata-read": {
            "kind": "ClusterRoleBinding",
            "role_ref": {"kind": "ClusterRole", "name": "public-coder-agent-cluster-metadata-reader"},
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
        ("public-coder-agent", "agentplane-testing-login-reader"),
        ("agentplane-testing", "agentplane-testing-operator"),
    } | (
        {("agentplane-staging", "claude-ai-coinbase-reader"), ("plaid-mcp", "plaid-spend-finance-config-reader")}
        if preset == "finance-agent"
        else set()
    )
    testing_config = yaml.safe_load(
        _by_name(agentplane_manifests[testing.ENV.namespace], "ConfigMap", "agentplane-app-config")["data"][
            "config.yaml"
        ]
    )
    assert "kubernetes_grants" not in testing_config["sandbox_presets"]["public-coder"]


@pytest.mark.parametrize("target_namespace", binding_delegation.external_scopes(staging.ENV))
def test_external_delegation_binds_exactly_the_granted_roles(
    target_namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    docs = agentplane_manifests[staging.ENV.namespace]
    config = yaml.safe_load(_by_name(docs, "ConfigMap", "agentplane-app-config")["data"]["config.yaml"])
    granted = {
        grant["role_ref"]["name"]
        for grant in config["kubernetes_grants"].values()
        if grant["kind"] == "RoleBinding"
        and grant["namespace"] == target_namespace
        and grant["role_ref"]["kind"] == "Role"
    }
    delegated = Cdk8sTesting.synth(binding_delegation.chart(Cdk8sTesting.app(), staging.ENV, target_namespace))
    role = one(doc for doc in delegated if doc["kind"] == "Role")
    assert {name for rule in role["rules"] if rule["verbs"] == ["bind"] for name in rule["resourceNames"]} == granted
    # An absent target namespace must not fail the app Kustomization's apply.
    assert not any(
        doc["kind"] in {"Role", "RoleBinding"}
        and doc["metadata"].get("namespace") == target_namespace
        and doc["metadata"]["name"] == role["metadata"]["name"]
        for doc in docs
    )


def test_environments_do_not_share_cluster_scoped_bundles(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    owners: dict[str, str] = {}
    for namespace, manifests in agentplane_manifests.items():
        for doc in manifests:
            if doc["kind"] != "Bundle":
                continue
            name = doc["metadata"]["name"]
            assert name not in owners, f"Bundle {name} is owned by both {owners.get(name)} and {namespace}"
            owners[name] = namespace


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_inference_is_granted_separately_from_platform_operations(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    docs = agentplane_manifests[namespace]
    granted = _by_name(docs, "EgressPolicy", INFERENCE_EXPERIMENTS_POLICY)
    basic = _by_name(docs, "EgressPolicy", BASIC_POLICY)
    config = _by_name(docs, "ConfigMap", "agentplane-app-config")
    assert INFERENCE_EXPERIMENTS_POLICY in yaml.safe_load(config["data"]["config.yaml"])["default_egress_policies"]
    # Standing caller identities and static OpenClaw must receive the separate grant too.
    for binding in (doc for doc in docs if doc["kind"] == "EgressBinding"):
        policies = binding["spec"]["policies"]
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
        credential = _by_name(docs, "EgressCredential", name)
        source = credential["spec"]["source"]["secretRef"]
        secret = _by_name(docs, "ExternalSecret", source["name"])
        assert secret["metadata"]["namespace"] == f"{namespace}-egress-credentials"
        assert source["key"] in {item["secretKey"] for item in secret["spec"]["data"]}


if __name__ == "__main__":
    pytest_bazel.main()


def test_finance_aiquota_history_is_read_only_and_finance_only(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    docs = agentplane_manifests[staging.ENV.namespace]
    config = yaml.safe_load(_by_name(docs, "ConfigMap", "agentplane-app-config")["data"]["config.yaml"])
    assert FINANCE_AIQUOTA_HISTORY_POLICY in config["sandbox_presets"]["finance-agent"]["egress_policies"]
    for name in ("public-coder", "haku"):
        assert FINANCE_AIQUOTA_HISTORY_POLICY not in config["sandbox_presets"][name]["egress_policies"]
    policy = _by_name(docs, "EgressPolicy", FINANCE_AIQUOTA_HISTORY_POLICY)
    assert policy["spec"]["rules"] == [
        {
            "hosts": [client.HTTP.fqdn],
            "clusterInternal": True,
            "methods": ["GET"],
            "paths": ["/"],
            "credentialRef": {"name": client.FINANCE_AGENT_CREDENTIALS},
        }
    ]
    credential = _by_name(docs, "EgressCredential", client.FINANCE_AGENT_CREDENTIALS)
    assert credential["spec"]["source"] == {
        "secretRef": {"name": client.FINANCE_AGENT_CREDENTIALS, "key": client.PASSWORD_KEY}
    }
    assert credential["spec"]["targets"] == [{"header": "Authorization", "method": "basicPassword"}]
    quota = _by_name(docs, "EgressPolicy", AIQUOTA_READ_POLICY)
    assert any(
        rule.get("clusterInternal")
        and rule.get("hosts") == ["aiquota-api.cli-proxy-api.svc.cluster.local"]
        and rule["methods"] == ["GET"]
        and rule["paths"] == ["/v1/quotas", "/v1/providers/*/raw"]
        for rule in quota["spec"]["rules"]
    )
