"""Standing access for the Haku workloads that share the haku-ui Identity."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting

from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.agentplane.actions_staging_policies import ANKIWEB_SYNC_POLICY, HAKU_NOTEBOOK_FETCH_POLICY
from cluster.cdk8s.haku import rbac as haku_rbac, ui_egress
from cluster.cdk8s.haku.namespace import NAMESPACE
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource


@pytest.fixture(scope="module")
def resources(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    app = scratch_app_factory()
    return [
        doc
        for chart in (staging.chart(app), haku_rbac.chart(app))
        for doc in cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    ]


def test_haku_ui_account_mounts_no_token(resources: list[dict[str, Any]]) -> None:
    """Haku runs Pods as this account; none of them gets a Kubernetes credential from it."""
    account = find_resource(resources, "ServiceAccount", ui_egress.SERVICE_ACCOUNT, namespace=NAMESPACE)

    assert account["automountServiceAccountToken"] is False


def test_haku_ui_grants_are_a_subset_of_claude_ais(resources: list[dict[str, Any]]) -> None:
    """`haku-sandbox-admin` lets Haku run Pods as haku-ui, so its grants must add nothing to claude-ai's."""
    haku_ui = find_resource(resources, "PolicyBinding", ui_egress.IDENTITY)
    claude_ai = find_resource(resources, "PolicyBinding", "claude-ai")

    assert haku_ui["spec"]["subject"] == {"identity": {"name": ui_egress.IDENTITY}}
    # Anki shares this account; the notebook policy's hosts are compared separately below.
    assert ANKIWEB_SYNC_POLICY in haku_ui["spec"]["policySets"]
    own = set(haku_ui["spec"]["policySets"]) - {ANKIWEB_SYNC_POLICY, HAKU_NOTEBOOK_FETCH_POLICY}
    assert own <= set(claude_ai["spec"]["policySets"])


def test_ankiweb_policy_presents_no_credential_and_reaches_only_ankiweb(resources: list[dict[str, Any]]) -> None:
    policy = find_resource(resources, "PolicySet", ANKIWEB_SYNC_POLICY)
    (rule,) = policy["spec"]["rules"]

    assert rule["type"] == "egress"
    assert rule["hosts"] == ["*.ankiweb.net"]
    # The real AnkiWeb credentials stay in the Pod; nothing is substituted, narrowed or cluster-internal.
    for absent in ("credentialRef", "methods", "paths", "clusterInternal"):
        assert absent not in rule, absent


def test_haku_anki_has_no_identity_or_binding_of_its_own(resources: list[dict[str, Any]]) -> None:
    """It acts as haku-ui on purpose (actions_staging_policies.py); a separate identity would be a design change."""
    bound_subjects = {
        d["spec"]["subject"].get("identity", d["spec"]["subject"])["name"]
        for d in resources
        if d["kind"] == "PolicyBinding"
    }

    assert "haku-anki" not in bound_subjects
    assert "haku-anki" not in {d["metadata"]["name"] for d in resources if d["kind"] == "Identity"}


def _egress_hosts(resources: list[dict[str, Any]], policy_sets: list[str]) -> set[str]:
    return {
        host
        for doc in resources
        if doc["kind"] == "PolicySet" and doc["metadata"]["name"] in policy_sets
        for rule in doc["spec"]["rules"]
        if rule["type"] == "egress"
        for host in rule["hosts"]
    }


def test_haku_jupyters_hosts_are_haku_uis_and_within_claude_ais(resources: list[dict[str, Any]]) -> None:
    """haku-jupyter egresses as haku-ui, so its notebook hosts are that account's, and add no host to claude-ai's."""
    haku_ui = find_resource(resources, "PolicyBinding", ui_egress.IDENTITY)
    claude_ai = find_resource(resources, "PolicyBinding", "claude-ai")

    assert HAKU_NOTEBOOK_FETCH_POLICY in haku_ui["spec"]["policySets"]
    hosts = _egress_hosts(resources, [HAKU_NOTEBOOK_FETCH_POLICY])
    assert hosts
    assert hosts <= _egress_hosts(resources, claude_ai["spec"]["policySets"])


def test_haku_ui_has_no_legacy_direct_action_policy(resources: list[dict[str, Any]]) -> None:
    assert not any(doc["kind"] == "PolicySet" and doc["metadata"]["name"] == ui_egress.IDENTITY for doc in resources)


def test_haku_sandbox_reaches_the_tls_proxy_and_actions(resources: list[dict[str, Any]]) -> None:
    egress = config_map_yaml(resources, "egress-go-settings", "settings.yaml")
    actions = config_map_yaml(resources, "actions-settings", "settings.yaml")

    assert NAMESPACE in egress["allowed_service_account_namespaces"]
    assert egress["forward"]["tls"]
    assert NAMESPACE in actions["caller_service_account_namespaces"]
    # Callers are watched in every allowed namespace, but policies and bindings only in the staging one.
    assert actions["policy_namespace"] == "agentplane-staging"


def _standing_records(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in resources if d["kind"] == "WorkloadCredential" and d["metadata"]["namespace"] == NAMESPACE]


def _granted(resources: list[dict[str, Any]], namespace: str, account: tuple[str, str]) -> set[tuple[str, str]]:
    """The (resource, verb) pairs Roles bound to `account` grant in `namespace`."""
    roles = {
        binding["roleRef"]["name"]
        for binding in resources
        if binding["kind"] == "RoleBinding"
        and binding["metadata"]["namespace"] == namespace
        and any((s.get("namespace"), s["name"]) == account for s in binding["subjects"])
    }
    return {
        (resource, verb)
        for role in resources
        if role["kind"] == "Role" and role["metadata"]["namespace"] == namespace and role["metadata"]["name"] in roles
        for rule in role["rules"]
        for resource in rule["resources"]
        for verb in rule["verbs"]
    }


def test_each_haku_workload_has_a_standing_record_as_the_haku_ui_identity(resources: list[dict[str, Any]]) -> None:
    """No owner and no binding make a record standing: its ID never rotates and its Secret is `svid-<name>`.
    Its subject names the Identity by name alone, since git cannot know the UID; a changed subject
    replaces the record, which the Flux annotation lets kustomize-controller do."""
    records = _standing_records(resources)

    assert {record["metadata"]["name"] for record in records} == set(ui_egress.STANDING_WORKLOADS)
    for record in records:
        assert "ownerReferences" not in record["metadata"]
        assert record["spec"] == {"subject": {"identity": {"name": ui_egress.IDENTITY}}}
        assert record["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"] == "enabled"


def test_the_issuer_serves_every_standing_records_namespace(resources: list[dict[str, Any]]) -> None:
    served = set(config_map_yaml(resources, "policy-controller-settings", "config.yaml")["workload_namespaces"])
    for namespace in {record["metadata"]["namespace"] for record in _standing_records(resources)}:
        assert namespace in served
        granted = _granted(resources, namespace, ("agentplane-staging", "policy-controller"))
        assert {
            ("workloadcredentials", "watch"),
            ("workloadcredentials/status", "update"),
            ("certificates", "create"),
        } <= granted


def test_the_proxy_resolves_every_standing_records_svid(resources: list[dict[str, Any]]) -> None:
    """The proxy reads the record, and its Identity in the policy namespace, live on each request; a
    missing grant fails closed."""
    settings = config_map_yaml(resources, "egress-go-settings", "settings.yaml")
    allowed = set(settings["allowed_service_account_namespaces"])
    policy_namespace = settings["rules_namespace"]
    identities = {(d["metadata"]["namespace"], d["metadata"]["name"]) for d in resources if d["kind"] == "Identity"}
    assert ("identities", "get") in _granted(resources, policy_namespace, ("agentplane-staging", "egress"))
    for record in _standing_records(resources):
        namespace = record["metadata"]["namespace"]
        assert namespace in allowed
        assert ("workloadcredentials", "get") in _granted(resources, namespace, ("agentplane-staging", "egress"))
        assert (policy_namespace, record["spec"]["subject"]["identity"]["name"]) in identities


if __name__ == "__main__":
    pytest_bazel.main()
