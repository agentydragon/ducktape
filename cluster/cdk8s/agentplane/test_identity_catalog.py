"""Relations the staging policy catalog holds: what each binding, grant and role set names exists,
and the Policy Controller may bind every role a role set names."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest_bazel
import yaml
from cdk8s import App, Testing as Cdk8sTesting  # Aliased: pytest would collect a bare `Testing`.
from more_itertools import one

from cluster.cdk8s.agentplane import binding_delegation, policy_controller, staging
from cluster.cdk8s.agentplane.egress import BASIC_POLICY, KUBERNETES_HOST

_RBAC_GROUP = "rbac.authorization.k8s.io"


def _of(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc["kind"] == kind]


def _names(docs: list[dict[str, Any]], kind: str) -> set[tuple[str, str]]:
    return {(doc["metadata"]["namespace"], doc["metadata"]["name"]) for doc in _of(docs, kind)}


def test_flux_bindings_and_launches_name_rendered_policy_sets(catalog_manifests: list[dict[str, Any]]) -> None:
    policy_sets = {name for _, name in _names(catalog_manifests, "PolicySet")}
    app_config = one(doc for doc in _of(catalog_manifests, "ConfigMap") if doc["metadata"]["name"] == "app-config")
    config = yaml.safe_load(app_config["data"]["config.yaml"])
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


def test_every_standing_record_names_a_rendered_identity(staging_manifests: list[dict[str, Any]]) -> None:
    """Every `WorkloadCredential` staging renders is standing and acts as an Identity the policy
    namespace renders, never as a ServiceAccount."""
    actions = one(doc for doc in _of(staging_manifests, "ConfigMap") if doc["metadata"]["name"] == "actions-settings")
    policy_namespace = yaml.safe_load(one(actions["data"].values()))["policy_namespace"]
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
    in the one namespace whose ServiceAccounts the proxy may impersonate, never as a stored secret,
    and is its account's one-rule use set. Every launch holds the shared account's use set."""
    docs = identity_catalog_manifests
    policy_sets = {doc["metadata"]["name"]: doc["spec"]["rules"] for doc in _of(docs, "PolicySet")}
    assert not [rule for rule in policy_sets[BASIC_POLICY] if KUBERNETES_HOST in rule.get("hosts", [])]
    credentials = {doc["metadata"]["name"]: doc["spec"]["source"] for doc in _of(docs, "EgressCredential")}
    impersonation = {
        doc["metadata"]["namespace"]
        for doc in _of(docs, "Role")
        for policy_rule in doc["rules"]
        if "impersonate" in policy_rule["verbs"] and "serviceaccounts" in policy_rule["resources"]
    }
    accounts = _names(docs, "ServiceAccount")
    account_sets = set()
    for name, rules in policy_sets.items():
        for rule in rules:
            if KUBERNETES_HOST not in rule.get("hosts", []):
                continue
            source = credentials[rule["credentialRef"]["name"]]
            assert set(source) == {"kubernetesServiceAccount"}, name
            account = source["kubernetesServiceAccount"]
            assert account["namespace"] in impersonation, name
            assert (account["namespace"], account["name"]) in accounts, name
            assert (name, rules) == (rule["credentialRef"]["name"], [rule])
            account_sets.add(name)
    assert len(impersonation) == 1
    app_config = one(doc for doc in _of(docs, "ConfigMap") if doc["metadata"]["name"] == "app-config")
    presets = yaml.safe_load(app_config["data"]["config.yaml"])["sandbox_presets"]
    # Every preset grants the shared diagnostics account's use set.
    assert all(account_sets & set(spec["policy_sets"]) for spec in presets.values()), presets


def _kubernetes_rules(rules: list[dict[str, Any]]) -> set[tuple[str, str, str | None]]:
    """(role kind, role name, namespace or None for cluster-wide) of each binding `rules` provision,
    whether written as `PolicySet` or as `KubernetesRoleSet` rules."""
    provisioned: set[tuple[str, str, str | None]] = set()
    for rule in rules:
        kind = rule["type"].removeprefix("kubernetes_")
        if kind == "cluster_role_binding":
            provisioned.add(("ClusterRole", rule["clusterRole"], None))
        elif kind == "role_binding":
            provisioned |= {(rule["role"]["kind"], rule["role"]["name"], ns) for ns in rule["namespaces"]}
    return provisioned


def _holders(docs: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The sets each launch preset and each Flux binding grants."""
    app_config = one(doc for doc in _of(docs, "ConfigMap") if doc["metadata"]["name"] == "app-config")
    presets = yaml.safe_load(app_config["data"]["config.yaml"])["sandbox_presets"]
    return {
        **{f"preset {name}": spec["policy_sets"] for name, spec in presets.items()},
        **{f"binding {doc['metadata']['name']}": doc["spec"]["policySets"] for doc in _of(docs, "PolicyBinding")},
    }


def _reachable(docs: list[dict[str, Any]], sets: list[str]) -> set[tuple[str, str, str | None]]:
    """What the accounts `sets` let their holder act as are granted, through each use rule's credential."""
    by_name = {doc["metadata"]["name"]: doc for doc in _of(docs, "PolicySet")}
    credentials = {doc["metadata"]["name"]: doc["spec"]["source"] for doc in _of(docs, "EgressCredential")}
    role_sets = {doc["metadata"]["name"]: doc["spec"]["rules"] for doc in _of(docs, "KubernetesRoleSet")}
    accounts = {
        (account["namespace"], account["name"])
        for name in sets
        for rule in by_name[name]["spec"]["rules"]
        if "credentialRef" in rule
        # Some credentials, such as the proxy's own `agentplane-workload`, are not objects.
        and (account := credentials.get(rule["credentialRef"]["name"], {}).get("kubernetesServiceAccount")) is not None
    }
    return {
        provisioned
        for grant in _of(docs, "ServiceAccountGrant")
        if (grant["spec"]["serviceAccount"]["namespace"], grant["spec"]["serviceAccount"]["name"]) in accounts
        for role_set in grant["spec"]["roleSets"]
        for provisioned in _kubernetes_rules(role_sets[role_set])
    }


def test_no_kubernetes_reach_is_lost_on_the_switch(
    staging_manifests: list[dict[str, Any]], identity_catalog_manifests: list[dict[str, Any]]
) -> None:
    """Every role binding a preset or a Flux binding provisions for its holder today is granted to
    an account the same holder may act as in the identity catalog."""
    today = {doc["metadata"]["name"]: doc["spec"]["rules"] for doc in _of(staging_manifests, "PolicySet")}
    granted = {
        holder: {provisioned for name in sets for provisioned in _kubernetes_rules(today[name])}
        for holder, sets in _holders(staging_manifests).items()
    }
    assert any(granted.values())
    after = _holders(identity_catalog_manifests)
    lost = {
        holder: sorted(rules - _reachable(identity_catalog_manifests, after[holder]), key=str)
        for holder, rules in granted.items()
    }
    assert {holder: rules for holder, rules in lost.items() if rules} == {}


def _bindable(docs: list[dict[str, Any]], *, namespace: str, account: str) -> set[tuple[str, str | None, str]]:
    """(kind, namespace or None for cluster-wide, name) of every role `account` may bind."""
    roles = {
        (doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"]): doc.get("rules", [])
        for doc in docs
        if doc["kind"] in {"Role", "ClusterRole"}
    }
    bindable: set[tuple[str, str | None, str]] = set()
    for binding in docs:
        if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        if not any(
            subject["kind"] == "ServiceAccount" and subject["name"] == account and subject.get("namespace") == namespace
            for subject in binding.get("subjects", [])
        ):
            continue
        scope = binding["metadata"]["namespace"] if binding["kind"] == "RoleBinding" else None
        ref = binding["roleRef"]
        for rule in roles[(ref["kind"], scope if ref["kind"] == "Role" else None, ref["name"])]:
            if "bind" not in rule["verbs"] or _RBAC_GROUP not in rule["apiGroups"]:
                continue
            for resource, kind in (("roles", "Role"), ("clusterroles", "ClusterRole")):
                if resource in rule["resources"]:
                    bindable |= {(kind, scope, name) for name in rule["resourceNames"]}
    return bindable


def test_controller_may_bind_every_role_a_role_set_names(
    scratch_app_factory: Callable[[], App], identity_catalog_manifests: list[dict[str, Any]]
) -> None:
    env = staging.environment(identity_catalog=True)
    grants = staging.assignable_grants()
    delegation = [
        doc
        for target in binding_delegation.external_scopes(env, grants)
        for doc in cast(
            list[dict[str, Any]],
            Cdk8sTesting.synth(binding_delegation.chart(scratch_app_factory(), env, target, grants)),
        )
    ]
    bindable = _bindable(
        [*identity_catalog_manifests, *delegation], namespace=env.namespace, account=policy_controller.NAME
    )
    needed: set[tuple[str, str, str]] = set()
    for role_set in _of(identity_catalog_manifests, "KubernetesRoleSet"):
        for rule in role_set["spec"]["rules"]:
            if rule["type"] == "cluster_role_binding":
                needed.add(("ClusterRole", "", rule["clusterRole"]))
            else:
                needed |= {(rule["role"]["kind"], namespace, rule["role"]["name"]) for namespace in rule["namespaces"]}
    assert needed
    unbindable = {
        (kind, namespace, name)
        for kind, namespace, name in needed
        if (kind, None, name) not in bindable and (kind, namespace or None, name) not in bindable
    }
    assert unbindable == set()


if __name__ == "__main__":
    pytest_bazel.main()
