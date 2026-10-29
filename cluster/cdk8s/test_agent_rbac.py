"""Agent RBAC relationships across the cdk8s charts that declare them."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s import agent_namespace_rbac, agent_rbac_base, agent_shared_rbac, ducktape_flux, loki_read_proxy
from cluster.cdk8s.agent_rbac_test_support import Permission, Rbac, uncovered
from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.clickhouse import installation as clickhouse_installation
from cluster.cdk8s.haku import rbac as haku_rbac
from cluster.cdk8s.haku.charts import console_chart
from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS, AgentReadable
from cluster.cdk8s.plaid_mcp import db as plaid_db, spend as plaid_spend
from cluster.cdk8s.public_coder import app as public_coder_app
from cluster.cdk8s.testing.scratch import scratch_app


@pytest.fixture(scope="module")
def model_resources(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    """Synthesize just the charts that declare agent RBAC and Agentplane's grants."""
    app = scratch_app(tmp_path_factory.mktemp("cdk8s"))
    charts = [
        agent_rbac_base.chart(app),
        agent_shared_rbac.chart(app),
        staging.chart(app),
        haku_rbac.chart(app),
        ducktape_flux.chart(app),
        plaid_db.chart(app),
        plaid_spend.chart(app),
        public_coder_app.rbac_chart(app),
        clickhouse_installation.agent_diagnostics_rbac_chart(app),
        console_chart(app),
    ]
    charts.extend(agent_namespace_rbac.chart(app, namespace) for namespace in NAMESPACE_DIAGNOSTICS)
    return [doc for chart in charts for doc in cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))]


@pytest.fixture(scope="module")
def agent_rbac(model_resources: list[dict[str, Any]]) -> Rbac:
    return Rbac(model_resources)


@pytest.fixture(scope="module")
def staging_app_config(model_resources: list[dict[str, Any]]) -> dict[str, Any]:
    app_config = one(
        doc for doc in model_resources if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "app-config"
    )
    config = yaml.safe_load(app_config["data"]["config.yaml"])
    assert isinstance(config, dict)
    return config


@pytest.fixture(scope="module")
def staging_policy_sets(model_resources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {doc["metadata"]["name"]: doc["spec"]["rules"] for doc in model_resources if doc["kind"] == "PolicySet"}


def one[T](values: Iterable[T]) -> T:
    (value,) = tuple(values)
    return value


def test_namespace_diagnostics_cover_static_and_managed_bindings(
    model_resources: list[dict[str, Any]],
    staging_app_config: dict[str, Any],
    staging_policy_sets: dict[str, list[dict[str, Any]]],
) -> None:
    expected = {
        (namespace, f"agent-readable-namespace-{level}")
        for namespace, classification in NAMESPACE_DIAGNOSTICS.items()
        for level in (("metadata", "logs") if classification is AgentReadable.LOGS else ("metadata",))
    }
    static_bindings = [
        doc
        for doc in model_resources
        if doc["kind"] == "RoleBinding"
        and doc["metadata"]["name"] in {"agent-diagnostics-metadata", "agent-diagnostics-logs"}
    ]
    assert all(doc["roleRef"]["kind"] == "ClusterRole" for doc in static_bindings)
    static = {(doc["metadata"]["namespace"], doc["roleRef"]["name"]) for doc in static_bindings}
    assert static == expected

    for preset in ("haku", "public-coder", "finance-agent"):
        selected_policy_sets = staging_app_config["sandbox_presets"][preset]["policy_sets"]
        managed = {
            (namespace, rule["role"]["name"])
            for name in selected_policy_sets
            for rule in staging_policy_sets[name]
            if rule["type"] == "kubernetes_role_binding"
            and rule["role"]["kind"] == "ClusterRole"
            and rule["role"]["name"] in {"agent-readable-namespace-metadata", "agent-readable-namespace-logs"}
            for namespace in rule["namespaces"]
        }
        assert managed == expected, preset


def test_agent_readable_log_namespaces_are_in_loki_anonymous_allowlist() -> None:
    expected = {
        namespace for namespace, classification in NAMESPACE_DIAGNOSTICS.items() if classification is AgentReadable.LOGS
    }
    assert expected <= set(loki_read_proxy.NAMESPACE_ALLOWLIST)


@pytest.mark.parametrize(
    ("kind", "name", "namespace"),
    [
        ("Group", "oidc-ksbx-groups:haku", ""),
        ("Group", "haku:access-profile:haku", ""),
        ("ServiceAccount", "haku", "haku-sandbox"),
    ],
)
def test_static_managed_agent_permission_parity(
    agent_rbac: Rbac,
    staging_app_config: dict[str, Any],
    staging_policy_sets: dict[str, list[dict[str, Any]]],
    kind: str,
    name: str,
    namespace: str,
) -> None:
    static = agent_rbac.identity(kind, name, namespace)
    managed = agent_rbac.managed(staging_app_config, staging_policy_sets, "haku", namespace="agentplane-staging")
    assert not uncovered(static, managed)
    assert not uncovered(managed, static)


def test_finance_spend_policy_role_is_named_get_only(
    agent_rbac: Rbac, staging_policy_sets: dict[str, list[dict[str, Any]]]
) -> None:
    role_rule = one(
        rule for rule in staging_policy_sets["spend-policy-config"] if rule["type"] == "kubernetes_role_binding"
    )
    assert role_rule == {
        "type": "kubernetes_role_binding",
        "role": {"kind": "Role", "name": "plaid-spend-finance-config-reader"},
        "namespaces": ["plaid-mcp"],
    }
    assert agent_rbac.rules("Role", role_rule["role"]["name"], "plaid-mcp") == {
        Permission("plaid-mcp", "", "secrets", "get", "plaid-spend-policy")
    }


def test_agent_permission_superset_and_finance_parity(
    agent_rbac: Rbac, staging_app_config: dict[str, Any], staging_policy_sets: dict[str, list[dict[str, Any]]]
) -> None:
    public = agent_rbac.managed(staging_app_config, staging_policy_sets, "public-coder", namespace="agentplane-staging")
    haku = agent_rbac.managed(staging_app_config, staging_policy_sets, "haku", namespace="agentplane-staging")
    finance = agent_rbac.managed(
        staging_app_config, staging_policy_sets, "finance-agent", namespace="agentplane-staging"
    )
    assert not uncovered(public, haku)
    assert not uncovered(public, finance)
    assert uncovered(finance, public) == {
        Permission("agentplane-staging", "", "secrets", "get", "coinbase-api-credentials"),
        Permission("plaid-mcp", "", "secrets", "get", "plaid-spend-policy"),
    }


@pytest.mark.parametrize("account", ["claude-ai", "haku-agent"])
def test_legacy_agentplane_accounts_are_not_haku_profile_aliases(agent_rbac: Rbac, account: str) -> None:
    expected: set[Permission] = set()
    if account == "claude-ai":
        for kind, name, namespace in (
            ("ClusterRole", "cluster-diagnostics-reader", None),
            ("Role", "claude-ai-coinbase-reader", "agentplane-staging"),
            ("Role", "claude-ai-sandbox-reader", "agentplane-staging"),
            ("Role", "agent-haku-console-metadata-reader", "haku-console"),
            ("Role", "agent-clickhouse-diagnostics-reader", "clickhouse"),
            ("Role", "public-coder-agent-reader", "public-coder-agent"),
            ("Role", "agent-public-coder-extended-diagnostics-reader", "public-coder-agent"),
        ):
            expected.update(agent_rbac.rules(kind, name, namespace))
        for namespace, classification in NAMESPACE_DIAGNOSTICS.items():
            expected.update(agent_rbac.rules("ClusterRole", "agent-readable-namespace-metadata", namespace))
            if classification is AgentReadable.LOGS:
                expected.update(agent_rbac.rules("ClusterRole", "agent-readable-namespace-logs", namespace))
    actual = agent_rbac.identity("ServiceAccount", account, "agentplane-staging")
    assert not uncovered(actual, expected)
    assert not uncovered(expected, actual)


def test_agent_permission_denials(
    agent_rbac: Rbac, staging_app_config: dict[str, Any], staging_policy_sets: dict[str, list[dict[str, Any]]]
) -> None:
    profiles = {
        name: agent_rbac.managed(staging_app_config, staging_policy_sets, name, namespace="agentplane-staging")
        for name in ("public-coder", "finance-agent", "haku")
    }
    profiles.update(
        {
            "claude-ai": agent_rbac.identity("ServiceAccount", "claude-ai", "agentplane-staging"),
            "haku-console": agent_rbac.identity("Group", "haku:access-profile:haku"),
            "haku-oidc": agent_rbac.identity("Group", "oidc-ksbx-groups:haku"),
            "haku-sa": agent_rbac.identity("ServiceAccount", "haku", "haku-sandbox"),
        }
    )
    denied = {
        Permission("public-coder-agent", "", "secrets", "list"),
        Permission("agentplane-staging", "", "pods/exec", "create"),
        Permission("agentplane-staging", "agents.x-k8s.io", "sandboxes", "create"),
        Permission("agentplane-staging", "agents.x-k8s.io", "sandboxes", "patch"),
        Permission("agentplane-staging", "agents.x-k8s.io", "sandboxes", "delete"),
    }
    denied |= {
        Permission("agentplane-staging", "agentplane.allegedly.works", "sandboxes", verb)
        for verb in ("create", "patch", "delete")
    }
    denied |= {
        Permission("agentplane-staging", "", "serviceaccounts/token", "create", "sandbox-controller"),
        Permission("agentplane-staging", "apps", "deployments", "patch", "app"),
        Permission("agentplane-staging", "apps", "deployments/scale", "patch", "runner-gateway"),
    }
    denied |= {
        Permission(namespace, "kustomize.toolkit.fluxcd.io", "kustomizations", "patch")
        for namespace in ("flux-system", "ducktape-flux", "agentplane-staging")
    }
    coinbase = Permission("agentplane-staging", "", "secrets", "get", "coinbase-api-credentials")
    denied |= {
        Permission("agentplane-staging", "", "secrets", verb)
        for verb in ("get", "list", "watch", "create", "update", "patch", "delete")
    }
    denied.add(Permission(None, "", "nodes/proxy", "get"))
    for name, permissions in profiles.items():
        assert bool(uncovered({coinbase}, permissions)) == (name == "public-coder"), name
        assert uncovered(denied, permissions) == denied, name
        if name == "claude-ai":
            sandbox_writes = {
                Permission("haku-sandbox", "", resource, verb)
                for resource in ("pods", "pods/exec", "secrets", "configmaps")
                for verb in ("create", "update", "patch", "delete")
            }
            assert uncovered(sandbox_writes, permissions) == sandbox_writes


def test_rule_coverage_is_not_role_name_equality() -> None:
    narrow = Permission("testing", "", "pods/log", "get")
    assert not uncovered({narrow}, {Permission(None, "*", "*", "*")})
    assert uncovered({Permission(None, "", "pods/log", "get")}, {narrow})
    assert uncovered({Permission("staging", "", "pods/log", "get")}, {narrow})
    assert uncovered({narrow}, {Permission("testing", "", "pods", "get")})
    assert uncovered({Permission("testing", "", "pods/exec", "create")}, {narrow})
    assert not uncovered({narrow}, {Permission("testing", "", "*/log", "get")})
    assert uncovered({narrow}, {Permission("testing", "", "pods/*", "get")})


def test_resource_names_do_not_mean_unrestricted_secret_access() -> None:
    named = Permission("testing", "", "secrets", "get", "testing-login")
    for forbidden in (
        Permission("testing", "", "secrets", "get"),
        Permission("testing", "", "secrets", "get", "staging-login"),
        Permission("testing", "", "secrets", "list"),
    ):
        assert uncovered({forbidden}, {named})
    assert not uncovered({named}, {Permission("testing", "", "secrets", "get")})


def test_non_resource_url_prefixes() -> None:
    prefix = Permission(None, "", "", "get", url="/healthz/*")
    assert prefix.covers(Permission(None, "", "", "get", url="/healthz/ready"))
    assert not prefix.covers(Permission(None, "", "", "get", url="/healthz"))
    assert not prefix.covers(Permission(None, "", "", "post", url="/healthz/ready"))
    assert not prefix.covers(Permission(None, "", "pods", "get"))


def test_binding_scope_and_subjects_are_resolved_separately() -> None:
    rbac = Rbac(
        [
            {
                "kind": "ClusterRole",
                "metadata": {"name": "reader"},
                "rules": [
                    {"apiGroups": [""], "resources": ["pods", "pods/log"], "verbs": ["get", "list"]},
                    {"nonResourceURLs": ["/healthz"], "verbs": ["get"]},
                ],
            },
            {
                "kind": "RoleBinding",
                "metadata": {"name": "read", "namespace": "testing"},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "reader"},
                "subjects": [{"kind": "Group", "name": "console"}],
            },
        ]
    )
    console = rbac.identity("Group", "console")
    assert len(console) == 4
    assert not rbac.identity("Group", "oidc"), "Do not union separate agent access paths"
    assert Permission("testing", "", "pods/log", "get") in console
    assert not any(permission.url for permission in console)
    with pytest.raises(KeyError):
        rbac.rules("Role", "reader", "testing")


def test_managed_profiles_include_standing_serviceaccount_group_bindings() -> None:
    rbac = Rbac(
        [
            {
                "kind": "Role",
                "metadata": {"name": "reader", "namespace": "testing"},
                "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}],
            },
            {
                "kind": "RoleBinding",
                "metadata": {"name": "group-read", "namespace": "testing"},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "reader"},
                "subjects": [{"kind": "Group", "name": "system:serviceaccounts:testing"}],
            },
        ]
    )
    config: dict[str, Any] = {"sandbox_presets": {"public": {"policy_sets": []}}}
    assert rbac.managed(config, {}, "public", namespace="testing") == {Permission("testing", "", "pods", "get")}
    assert not rbac.managed(config, {}, "public", namespace="staging")


def test_aggregation_is_not_silently_ignored() -> None:
    rbac = Rbac(
        [{"kind": "ClusterRole", "metadata": {"name": "aggregate"}, "aggregationRule": {"clusterRoleSelectors": []}}]
    )
    with pytest.raises(AssertionError, match="aggregated"):
        rbac.rules("ClusterRole", "aggregate", None)


if __name__ == "__main__":
    pytest_bazel.main()
