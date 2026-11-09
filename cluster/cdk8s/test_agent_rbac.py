"""Agent RBAC relationships across the cdk8s charts that declare them, under both renderings of
Agentplane's policy catalog: PolicySets binding roles to sandbox ServiceAccounts, and the identity
catalog (`Environment.identity_catalog`), where holders reach Kubernetes only by acting as accounts.
`cluster/cdk8s/agentplane/test_identity_catalog.py` holds the catalog's own relations: referenced
objects exist, the Policy Controller may bind every role a role set names, and no binding a holder
has today is lost on the switch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Testing as Cdk8sTesting
from more_itertools import one

from cluster.cdk8s import agent_namespace_rbac, agent_rbac_base, agent_shared_rbac, ducktape_flux, loki_read_proxy
from cluster.cdk8s.agent_rbac_test_support import BoundRole, Permission, Rbac, uncovered
from cluster.cdk8s.agentplane import egress, staging
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace
from cluster.cdk8s.clickhouse import installation as clickhouse_installation
from cluster.cdk8s.haku import rbac as haku_rbac
from cluster.cdk8s.haku.charts import console_chart
from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS, AgentReadable
from cluster.cdk8s.plaid_mcp import db as plaid_db, spend as plaid_spend
from cluster.cdk8s.public_coder import app as public_coder_app

_NAMESPACE = staging.ENV.namespace
_ACCOUNTS_NAMESPACE = kubernetes_accounts_namespace(_NAMESPACE)


@dataclass(frozen=True)
class Rendering:
    resources: list[dict[str, Any]]
    rbac: Rbac
    # Preset -> the policy sets a launch of it holds, the environment defaults included.
    launches: dict[str, list[str]]
    # Flux PolicyBinding name -> its spec.
    bindings: dict[str, dict[str, Any]]

    def launch(self, preset: str) -> set[Permission]:
        return self.rbac.launch(self.launches[preset], namespace=_NAMESPACE)

    def binding(self, name: str) -> set[Permission]:
        return self.rbac.held(self.bindings[name]["policySets"])


def _rendering(scratch_app_factory: Callable[[], App], *, identity_catalog: bool) -> Rendering:
    """Synthesize just the charts that declare agent RBAC and Agentplane's grants."""
    app = scratch_app_factory()
    charts = [
        agent_rbac_base.chart(app),
        agent_shared_rbac.chart(app),
        staging.chart(app, staging.environment(identity_catalog=identity_catalog)),
        haku_rbac.chart(app),
        ducktape_flux.chart(app),
        plaid_db.chart(app),
        plaid_spend.chart(app),
        public_coder_app.rbac_chart(app),
        clickhouse_installation.agent_diagnostics_rbac_chart(app),
        console_chart(app),
    ]
    charts.extend(agent_namespace_rbac.chart(app, namespace) for namespace in NAMESPACE_DIAGNOSTICS)
    # Its own app: it names its chart "agentplane" too.
    charts.append(staging.delivery_chart(scratch_app_factory()))
    docs = [doc for chart in charts for doc in cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))]

    def config_map(name: str, key: str) -> Any:
        return yaml.safe_load(
            one(doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == name)["data"][key]
        )

    defaults = config_map("runner-gateway-settings", "settings.yaml")["environment_default_policy_sets"]
    presets = config_map("app-config", "config.yaml")["sandbox_presets"]
    bindings = [doc for doc in docs if doc["kind"] == "PolicyBinding"]
    assert len({doc["metadata"]["name"] for doc in bindings}) == len(bindings)
    return Rendering(
        resources=docs,
        rbac=Rbac(docs),
        launches={name: list(dict.fromkeys([*defaults, *spec["policy_sets"]])) for name, spec in presets.items()},
        bindings={doc["metadata"]["name"]: doc["spec"] for doc in bindings},
    )


@pytest.fixture(scope="module")
def serviceaccount_rendering(scratch_app_factory: Callable[[], App]) -> Rendering:
    return _rendering(scratch_app_factory, identity_catalog=False)


@pytest.fixture(scope="module")
def identity_rendering(scratch_app_factory: Callable[[], App]) -> Rendering:
    return _rendering(scratch_app_factory, identity_catalog=True)


@pytest.fixture(scope="module", params=[False, True], ids=["serviceaccount-catalog", "identity-catalog"])
def rendering(request: pytest.FixtureRequest) -> Rendering:
    return cast(
        Rendering, request.getfixturevalue("identity_rendering" if request.param else "serviceaccount_rendering")
    )


def test_namespace_diagnostics_cover_static_and_managed_bindings(rendering: Rendering) -> None:
    expected = {
        (namespace, f"agent-readable-namespace-{level}")
        for namespace, classification in NAMESPACE_DIAGNOSTICS.items()
        for level in (("metadata", "logs") if classification is AgentReadable.LOGS else ("metadata",))
    }
    readers = {"agent-readable-namespace-metadata", "agent-readable-namespace-logs"}
    static_bindings = [
        doc
        for doc in rendering.resources
        if doc["kind"] == "RoleBinding"
        and doc["metadata"]["name"] in {"agent-diagnostics-metadata", "agent-diagnostics-logs"}
    ]
    assert all(doc["roleRef"]["kind"] == "ClusterRole" for doc in static_bindings)
    static = {(doc["metadata"]["namespace"], doc["roleRef"]["name"]) for doc in static_bindings}
    assert static == expected

    assert rendering.launches
    for preset, sets in rendering.launches.items():
        managed = {
            (role.namespace, role.name)
            for role in rendering.rbac.held_roles(sets)
            if role.kind == "ClusterRole" and role.name in readers
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
def test_static_managed_agent_permission_parity(rendering: Rendering, kind: str, name: str, namespace: str) -> None:
    static = rendering.rbac.identity(kind, name, namespace)
    managed = rendering.launch("haku")
    assert not uncovered(static, managed)
    assert not uncovered(managed, static)


def test_finance_spend_policy_role_is_named_get_only(rendering: Rendering) -> None:
    role = BoundRole("Role", "plaid-spend-finance-config-reader", "plaid-mcp")
    assert rendering.rbac.set_roles("spend-policy-config") == {role}
    assert rendering.rbac.bound({role}) == {Permission("plaid-mcp", "", "secrets", "get", "plaid-spend-policy")}


def test_agent_permission_superset_and_finance_parity(rendering: Rendering) -> None:
    public = rendering.launch("public-coder")
    haku = rendering.launch("haku")
    finance = rendering.launch("finance-agent")
    assert not uncovered(public, haku)
    assert not uncovered(public, finance)
    assert uncovered(finance, public) == {
        Permission(_NAMESPACE, "", "secrets", "get", "coinbase-api-credentials"),
        Permission("plaid-mcp", "", "secrets", "get", "plaid-spend-policy"),
    }


@pytest.mark.parametrize("account", ["claude-ai", "haku-agent"])
def test_legacy_agentplane_accounts_are_not_haku_profile_aliases(rendering: Rendering, account: str) -> None:
    agent_rbac = rendering.rbac
    expected: set[Permission] = set()
    if account == "claude-ai":
        for kind, name, namespace in (
            ("ClusterRole", "cluster-diagnostics-reader", None),
            ("Role", "claude-ai-coinbase-reader", _NAMESPACE),
            ("Role", "claude-ai-sandbox-reader", _NAMESPACE),
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
    actual = agent_rbac.identity("ServiceAccount", account, _NAMESPACE)
    assert not uncovered(actual, expected)
    assert not uncovered(expected, actual)


def test_agent_permission_denials(rendering: Rendering) -> None:
    rbac = rendering.rbac
    profiles = {name: rendering.launch(name) for name in ("public-coder", "finance-agent", "haku")}
    profiles.update(
        {
            # Each standing caller's ServiceAccount keeps its own bindings in both renderings.
            caller: rbac.identity("ServiceAccount", caller, _NAMESPACE) | rendering.binding(caller)
            for caller in ("claude-ai", "haku-agent")
        }
    )
    profiles.update(
        {
            "haku-console": rbac.identity("Group", "haku:access-profile:haku"),
            "haku-oidc": rbac.identity("Group", "oidc-ksbx-groups:haku"),
            "haku-sa": rbac.identity("ServiceAccount", "haku", "haku-sandbox"),
        }
    )
    denied = {
        Permission("public-coder-agent", "", "secrets", "list"),
        Permission(_NAMESPACE, "", "pods/exec", "create"),
        Permission(_NAMESPACE, "agents.x-k8s.io", "sandboxes", "create"),
        Permission(_NAMESPACE, "agents.x-k8s.io", "sandboxes", "patch"),
        Permission(_NAMESPACE, "agents.x-k8s.io", "sandboxes", "delete"),
    }
    denied |= {
        Permission(_NAMESPACE, "agentplane.allegedly.works", "sandboxes", verb)
        for verb in ("create", "patch", "delete")
    }
    denied |= {
        Permission(_NAMESPACE, "", "serviceaccounts/token", "create", "sandbox-controller"),
        Permission(_NAMESPACE, "apps", "deployments", "patch", "app"),
        Permission(_NAMESPACE, "apps", "deployments/scale", "patch", "runner-gateway"),
    }
    denied |= {
        Permission(namespace, "kustomize.toolkit.fluxcd.io", "kustomizations", "patch")
        for namespace in ("flux-system", "ducktape-flux", _NAMESPACE)
    }
    # No agent may reach another account: not by impersonation, a token, or a grant of its own.
    denied |= {Permission(None, "", resource, "impersonate") for resource in ("serviceaccounts", "users", "groups")}
    denied |= {
        Permission(_ACCOUNTS_NAMESPACE, "", "serviceaccounts", "impersonate"),
        Permission(_ACCOUNTS_NAMESPACE, "", "serviceaccounts/token", "create"),
        Permission(_NAMESPACE, "agentplane.allegedly.works", "serviceaccountgrants", "create"),
        Permission(_NAMESPACE, "agentplane.allegedly.works", "serviceaccountgrants", "patch"),
    }
    coinbase = Permission(_NAMESPACE, "", "secrets", "get", "coinbase-api-credentials")
    denied |= {
        Permission(_NAMESPACE, "", "secrets", verb)
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


def test_egress_proxy_impersonates_only_and_every_account(rendering: Rendering) -> None:
    """The proxy's resolved bindings, wildcards and ClusterRoles included, let it impersonate only
    ServiceAccounts of the accounts namespace (`test_identity_catalog` checks the Role that says
    so), and each rendered account credential names one it may impersonate."""
    proxy = rendering.rbac.identity("ServiceAccount", egress.NAME, _NAMESPACE)
    impersonation = {permission for permission in proxy if permission.verb in {"impersonate", "*"}}
    assert impersonation
    assert not uncovered(impersonation, {Permission(_ACCOUNTS_NAMESPACE, "", "serviceaccounts", "impersonate")})
    accounts = {
        Permission(account["namespace"], "", "serviceaccounts", "impersonate", account["name"])
        for source in rendering.rbac.credentials.values()
        if (account := source.get("kubernetesServiceAccount")) is not None
    }
    assert not uncovered(accounts, proxy)


def test_accounts_hold_only_their_grants(identity_rendering: Rendering) -> None:
    """No rendered binding names an account or its namespace, so beyond what every ServiceAccount
    holds, an account's RoleBindings are what the Policy Controller expands its grants' role sets
    to (`test_identity_catalog` checks it may bind each)."""
    accounts = [
        doc["metadata"]["name"]
        for doc in identity_rendering.resources
        if doc["kind"] == "ServiceAccount" and doc["metadata"]["namespace"] == _ACCOUNTS_NAMESPACE
    ]
    assert accounts
    for name in accounts:
        direct = identity_rendering.rbac.subjects(
            {
                ("ServiceAccount", name, _ACCOUNTS_NAMESPACE),
                ("User", f"system:serviceaccount:{_ACCOUNTS_NAMESPACE}:{name}", ""),
                ("Group", f"system:serviceaccounts:{_ACCOUNTS_NAMESPACE}", ""),
            }
        )
        assert direct == set(), name


def test_no_preset_gains_access_on_the_switch(
    serviceaccount_rendering: Rendering, identity_rendering: Rendering
) -> None:
    """The accounts a preset's launch acts as let it do no more than its PolicySets bound for it:
    the converse of `test_identity_catalog`'s no reach lost on the switch."""
    assert identity_rendering.launches.keys() == serviceaccount_rendering.launches.keys()
    gained = {
        preset: sorted(map(str, uncovered(identity_rendering.launch(preset), serviceaccount_rendering.launch(preset))))
        for preset in identity_rendering.launches
    }
    assert {preset: permissions for preset, permissions in gained.items() if permissions} == {}


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
    assert rbac.launch([], namespace="testing") == {Permission("testing", "", "pods", "get")}
    assert not rbac.launch([], namespace="staging")


def test_aggregation_is_not_silently_ignored() -> None:
    rbac = Rbac(
        [{"kind": "ClusterRole", "metadata": {"name": "aggregate"}, "aggregationRule": {"clusterRoleSelectors": []}}]
    )
    with pytest.raises(AssertionError, match="aggregated"):
        rbac.rules("ClusterRole", "aggregate", None)


if __name__ == "__main__":
    pytest_bazel.main()
