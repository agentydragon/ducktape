"""Relations the staging policy catalog holds: what each binding, grant and role set names exists,
and the Policy Controller may bind every role a role set names."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import ApiObject, App
from more_itertools import one

from cluster.cdk8s import agent_access_profiles
from cluster.cdk8s.agent_rbac_test_support import Permission, Rbac, uncovered
from cluster.cdk8s.agentplane import binding_delegation, egress, kubernetes_account_ref, policy_controller, staging
from cluster.cdk8s.agentplane.actions_staging_policies import COMMON_GITHUB_READ_SET
from cluster.cdk8s.agentplane.egress import BASIC_POLICY, KUBERNETES_HOST
from cluster.cdk8s.public_coder import egress as public_coder_egress
from cluster.cdk8s.testing.manifests import config_map_yaml

_RBAC_GROUP = "rbac.authorization.k8s.io"


def _of(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc["kind"] == kind]


def _names(docs: list[dict[str, Any]], kind: str) -> set[tuple[str, str]]:
    return {(doc["metadata"]["namespace"], doc["metadata"]["name"]) for doc in _of(docs, kind)}


def test_flux_bindings_and_launches_name_rendered_policy_sets(catalog_manifests: list[dict[str, Any]]) -> None:
    policy_sets = {name for _, name in _names(catalog_manifests, "PolicySet")}
    config = config_map_yaml(catalog_manifests, "app-config", "config.yaml")
    named = {
        (f"preset {preset}", name) for preset, spec in config["sandbox_presets"].items() for name in spec["policy_sets"]
    }
    named |= {
        (f"binding {binding['metadata']['name']}", name)
        for binding in _of(catalog_manifests, "PolicyBinding")
        for name in binding["spec"]["policySets"]
    }
    assert named
    assert {(holder, name) for holder, name in named if name not in policy_sets} == set()


def test_no_policy_set_carries_a_kubernetes_rule(identity_catalog_manifests: list[dict[str, Any]]) -> None:
    assert _of(identity_catalog_manifests, "KubernetesRoleSet")
    assert [
        (doc["metadata"]["name"], rule["type"])
        for doc in _of(identity_catalog_manifests, "PolicySet")
        for rule in doc["spec"]["rules"]
        if rule["type"].startswith("kubernetes_")
    ] == []


def test_every_identity_binding_names_a_rendered_identity(staging_manifests: list[dict[str, Any]]) -> None:
    """Staging renders the standing workloads' bindings on Identities before the catalog; each names
    one staging renders, in the binding's namespace."""
    identities = _names(staging_manifests, "Identity")
    named = [
        (binding["metadata"]["namespace"], binding["spec"]["subject"]["identity"]["name"])
        for binding in _of(staging_manifests, "PolicyBinding")
        if "identity" in binding["spec"]["subject"]
    ]
    assert named
    assert set(named) <= identities


def test_standing_callers_always_use_identity_policies_and_exact_account_grants(
    staging_manifests: list[dict[str, Any]], identity_catalog_manifests: list[dict[str, Any]]
) -> None:
    """Backend Identities, effective policy rules, and exact Kubernetes grants ignore the optional catalog."""
    assert not staging.ENV.identity_catalog
    effective_rules: dict[str, list[list[dict[str, Any]]]] = {"claude-ai": [], "haku-agent": []}
    expected_role_set_rules = {
        "coinbase": [
            {
                "namespaces": [staging.ENV.namespace],
                "role": {"kind": "Role", "name": "claude-ai-coinbase-reader"},
                "type": "role_binding",
            }
        ],
        "plaid-diagnostics": [
            {
                "namespaces": ["plaid-mcp"],
                "role": {"kind": "Role", "name": "plaid-finance-diagnostics-reader"},
                "type": "role_binding",
            }
        ],
    }
    for docs in (staging_manifests, identity_catalog_manifests):
        identities = _names(docs, "Identity")
        policy_sets = {doc["metadata"]["name"]: doc["spec"]["rules"] for doc in _of(docs, "PolicySet")}
        role_sets = {doc["metadata"]["name"]: doc["spec"]["rules"] for doc in _of(docs, "KubernetesRoleSet")}
        credentials = {doc["metadata"]["name"]: doc["spec"]["source"] for doc in _of(docs, "EgressCredential")}
        grants = _of(docs, "ServiceAccountGrant")

        for name, rule_sets in effective_rules.items():
            assert (staging.ENV.namespace, name) in identities
            binding = one(doc for doc in _of(docs, "PolicyBinding") if doc["metadata"]["name"] == name)
            assert binding["spec"]["subject"] == {"identity": {"name": name}}
            selected = binding["spec"]["policySets"]
            rules = [policy_sets[policy] for policy in selected]
            assert all(not rule["type"].startswith("kubernetes_") for policy in rules for rule in policy)
            rule_sets.append(rules)

            account = kubernetes_account_ref.account(staging.ENV.namespace, name)
            assert account.credential_name in selected
            assert credentials[account.credential_name] == {
                "kubernetesServiceAccount": {"name": account.name, "namespace": account.namespace}
            }
            grant = one(
                doc
                for doc in grants
                if doc["spec"]["serviceAccount"] == {"name": account.name, "namespace": account.namespace}
            )
            expected_role_sets = set(expected_role_set_rules)
            if name == "claude-ai":
                expected_role_sets.add(agent_access_profiles.CLAUDE_AI_STATIC_ROLE_SET)
            assert set(grant["spec"]["roleSets"]) == expected_role_sets
            assert {name: role_sets[name] for name in expected_role_set_rules} == expected_role_set_rules

    assert all(rules[0] == rules[1] for rules in effective_rules.values())


def test_openclaw_identity_and_grants_are_independent_of_the_catalog_switch(
    catalog_manifests: list[dict[str, Any]],
) -> None:
    """The paused OpenClaw backend keeps its reviewed Identity grants in either policy rendering."""
    identity = public_coder_egress.OPENCLAW.identity
    assert (staging.ENV.namespace, identity) in _names(catalog_manifests, "Identity")
    assert {
        binding["metadata"]["name"]: binding["spec"]["policySets"]
        for binding in _of(catalog_manifests, "PolicyBinding")
        if binding["metadata"]["namespace"] == staging.ENV.namespace
        and binding["spec"]["subject"] == {"identity": {"name": identity}}
    } == {
        public_coder_egress.POLICY: [
            public_coder_egress.POLICY,
            egress.INFERENCE_EXPERIMENTS_POLICY,
            egress.PUBLIC_INTERNET_POLICY,
        ],
        "public-coder-openclaw-actions": [COMMON_GITHUB_READ_SET],
    }


def test_every_standing_record_names_a_rendered_identity(staging_manifests: list[dict[str, Any]]) -> None:
    """Every `WorkloadCredential` staging renders is standing and acts as an Identity the policy
    namespace renders, never as a ServiceAccount."""
    policy_namespace = config_map_yaml(staging_manifests, "actions-settings", "settings.yaml")["policy_namespace"]
    identities = _names(staging_manifests, "Identity")
    records = _of(staging_manifests, "WorkloadCredential")
    assert records
    for record in records:
        assert set(record["spec"]) == {"subject"}, record["metadata"]
        assert set(record["spec"]["subject"]) == {"identity"}, record["metadata"]
        assert (policy_namespace, record["spec"]["subject"]["identity"]["name"]) in identities, record["metadata"]


def test_every_binding_names_an_identity_that_exists(identity_catalog_manifests: list[dict[str, Any]]) -> None:
    bindings = _of(identity_catalog_manifests, "PolicyBinding")
    assert bindings
    identities = _names(identity_catalog_manifests, "Identity")
    for binding in bindings:
        subject = binding["spec"]["subject"]
        assert set(subject) == {"identity"}, binding["metadata"]["name"]
        assert (binding["metadata"]["namespace"], subject["identity"]["name"]) in identities, binding["metadata"]


def test_every_grant_names_role_sets_and_an_account_that_exist(
    identity_catalog_manifests: list[dict[str, Any]],
) -> None:
    grants = _of(identity_catalog_manifests, "ServiceAccountGrant")
    assert grants
    role_sets = _names(identity_catalog_manifests, "KubernetesRoleSet")
    accounts = _names(identity_catalog_manifests, "ServiceAccount")
    for grant in grants:
        namespace = grant["metadata"]["namespace"]
        assert {(namespace, name) for name in grant["spec"]["roleSets"]} <= role_sets, grant["metadata"]
        account = grant["spec"]["serviceAccount"]
        assert (account["namespace"], account["name"]) in accounts, grant["metadata"]


def test_api_server_is_reached_only_as_rendered_accounts(identity_catalog_manifests: list[dict[str, Any]]) -> None:
    """`basic` holds no API server rule; every rule that does acts as an account the platform renders
    and is its account's one-rule use set. Every launch holds the shared account's use set.
    test_agent_rbac checks the proxy's effective impersonation permissions."""
    docs = identity_catalog_manifests
    policy_sets = {doc["metadata"]["name"]: doc["spec"]["rules"] for doc in _of(docs, "PolicySet")}
    assert not [rule for rule in policy_sets[BASIC_POLICY] if KUBERNETES_HOST in rule.get("hosts", [])]
    credentials = {doc["metadata"]["name"]: doc["spec"]["source"] for doc in _of(docs, "EgressCredential")}
    accounts = _names(docs, "ServiceAccount")
    account_sets = set()
    for name, rules in policy_sets.items():
        for rule in rules:
            if KUBERNETES_HOST not in rule.get("hosts", []):
                continue
            source = credentials[rule["credentialRef"]["name"]]
            assert set(source) == {"kubernetesServiceAccount"}, name
            account = source["kubernetesServiceAccount"]
            assert (account["namespace"], account["name"]) in accounts, name
            assert (name, rules) == (rule["credentialRef"]["name"], [rule])
            account_sets.add(name)
    presets = config_map_yaml(docs, "app-config", "config.yaml")["sandbox_presets"]
    # Every preset grants the shared diagnostics account's use set.
    assert all(account_sets & set(spec["policy_sets"]) for spec in presets.values()), presets


@pytest.fixture(scope="module")
def external_delegation(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    app = scratch_app_factory()
    charts = binding_delegation.external_charts(staging.ENV, staging.assignable_grants())
    objects: list[dict[str, Any]] = []
    for build in charts.values():
        chart = build(app)
        objects.extend(cast(ApiObject, obj).to_json() for obj in chart.node.find_all() if ApiObject.is_api_object(obj))
    return objects


def test_external_delegation_is_separate_from_the_app(
    catalog_manifests: list[dict[str, Any]], external_delegation: list[dict[str, Any]]
) -> None:
    # An absent external namespace must not fail the app Kustomization's apply.
    for kind in ("Role", "RoleBinding"):
        assert _names(catalog_manifests, kind).isdisjoint(_names(external_delegation, kind))


def test_controller_may_bind_every_role_a_role_set_names(
    identity_catalog_manifests: list[dict[str, Any]], external_delegation: list[dict[str, Any]]
) -> None:
    rbac = Rbac([*identity_catalog_manifests, *external_delegation])
    subject = {("ServiceAccount", policy_controller.NAME, staging.ENV.namespace)}
    direct = rbac.subjects(subject)
    bindable = {
        permission
        for permission in direct
        if permission.api_group == _RBAC_GROUP
        and permission.verb == "bind"
        and permission.resource in {"roles", "clusterroles"}
    }
    assert all(permission.name is not None for permission in bindable), "bind grants must name their roles"
    needed = {
        Permission(role.namespace, _RBAC_GROUP, "roles" if role.kind == "Role" else "clusterroles", "bind", role.name)
        for name in rbac.role_sets
        for role in rbac.set_roles(name)
    }
    assert needed
    assert not uncovered(needed, bindable)
    # Other namespaces delegate exactly the named Roles assigned by the catalog.
    assert {p for p in Rbac(external_delegation).subjects(subject) if p.verb in {"bind", "*"}} == {
        p for p in needed if p.resource == "roles" and p.namespace != staging.ENV.namespace
    }


if __name__ == "__main__":
    pytest_bazel.main()
