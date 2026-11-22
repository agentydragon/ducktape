"""Standing access for the Haku workloads that share the haku-ui Identity."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import pytest_bazel
from cdk8s import App

from agentplane.crd_group import GROUP
from cluster.cdk8s.agent_rbac_test_support import Permission, Rbac, uncovered
from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.agentplane.actions_staging_policies import ANKIWEB_SYNC_POLICY, HAKU_NOTEBOOK_FETCH_POLICY
from cluster.cdk8s.haku import rbac as haku_rbac, ui_egress
from cluster.cdk8s.haku.namespace import NAMESPACE
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource


@pytest.fixture(scope="module")
def resources(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    return staging.chart(scratch_app_factory()).to_json() + haku_rbac.chart(scratch_app_factory()).to_json()


def test_haku_ui_account_mounts_no_token(resources: list[dict[str, Any]]) -> None:
    """Haku runs Pods as this account; none of them gets a Kubernetes credential from it."""
    account = find_resource(resources, "ServiceAccount", ui_egress.SERVICE_ACCOUNT, namespace=NAMESPACE)

    assert account["automountServiceAccountToken"] is False


def _bound_sets(resources: list[dict[str, Any]], identity: str) -> set[str]:
    return {
        name
        for binding in resources
        if binding["kind"] == "PolicyBinding"
        and binding["metadata"]["namespace"] == staging.ENV.namespace
        and binding["spec"]["subject"].get("identity", {}).get("name") == identity
        for name in binding["spec"]["policySets"]
    }


def test_haku_ui_standing_access_stays_within_claude_ais_except_ankiweb(resources: list[dict[str, Any]]) -> None:
    """Haku's sandbox admin can run Pods as haku-ui. Compare every binding on that Identity."""
    haku = _bound_sets(resources, ui_egress.IDENTITY)
    claude = _bound_sets(resources, "claude-ai")
    special = {ANKIWEB_SYNC_POLICY, HAKU_NOTEBOOK_FETCH_POLICY}
    assert special <= haku
    assert haku - special <= claude

    read_methods = {"GET", "HEAD"}
    # Notebook downloads can request any path. A path-scoped Claude grant cannot cover them.
    claude_reads = {
        (host, method)
        for policy in resources
        if policy["kind"] == "PolicySet"
        and policy["metadata"]["namespace"] == staging.ENV.namespace
        and policy["metadata"]["name"] in claude
        for rule in policy["spec"]["rules"]
        if rule["type"] == "egress" and not rule.get("paths")
        for host in rule["hosts"]
        for method in rule.get("methods", read_methods)
    }
    notebook = find_resource(resources, "PolicySet", HAKU_NOTEBOOK_FETCH_POLICY, namespace=staging.ENV.namespace)
    assert notebook["spec"]["rules"]
    for rule in notebook["spec"]["rules"]:
        assert rule["type"] == "egress"
        assert rule["hosts"]
        assert rule["methods"]
        assert set(rule["methods"]) <= read_methods
        assert {(host, method) for host in rule["hosts"] for method in rule["methods"]} <= claude_reads
        assert "credentialRef" not in rule
        assert not rule.get("clusterInternal", False)


def test_ankiweb_policy_presents_no_credential_and_reaches_only_ankiweb(resources: list[dict[str, Any]]) -> None:
    policy = find_resource(resources, "PolicySet", ANKIWEB_SYNC_POLICY, namespace=staging.ENV.namespace)
    (rule,) = policy["spec"]["rules"]

    assert rule["type"] == "egress"
    assert rule["hosts"] == ["*.ankiweb.net"]
    # The real AnkiWeb credentials stay in the Pod; nothing is substituted, narrowed or cluster-internal.
    for absent in ("credentialRef", "methods", "paths", "clusterInternal"):
        assert absent not in rule, absent


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


def test_each_haku_workload_has_a_standing_record_as_the_haku_ui_identity(resources: list[dict[str, Any]]) -> None:
    """No owner and no binding make a record standing: its ID never rotates and its Secret is `svid-<name>`.
    Its subject names the Identity by name alone, since git cannot know the UID; a changed subject
    replaces the record, which the Flux annotation lets kustomize-controller do."""
    records = _standing_records(resources)

    record_names = {record["metadata"]["name"] for record in records}
    # haku-state mounts svid-haku-anki. Its subject determines which bindings Anki can use.
    assert "haku-anki" in record_names
    assert record_names == set(ui_egress.STANDING_WORKLOADS)
    for record in records:
        assert "ownerReferences" not in record["metadata"]
        assert record["spec"] == {"subject": {"identity": {"name": ui_egress.IDENTITY}}}
        assert record["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"] == "enabled"


def test_the_issuer_serves_every_standing_records_namespace(resources: list[dict[str, Any]]) -> None:
    served = set(config_map_yaml(resources, "policy-controller-settings", "config.yaml")["workload_namespaces"])
    assert NAMESPACE in served
    permissions = Rbac(resources).identity("ServiceAccount", "policy-controller", staging.ENV.namespace)
    required = {
        Permission(NAMESPACE, GROUP, "workloadcredentials", "watch"),
        Permission(NAMESPACE, "cert-manager.io", "certificates", "create"),
        *(
            Permission(NAMESPACE, GROUP, "workloadcredentials/status", "update", record["metadata"]["name"])
            for record in _standing_records(resources)
        ),
    }
    assert not uncovered(required, permissions)


def test_the_proxy_resolves_every_standing_records_svid(resources: list[dict[str, Any]]) -> None:
    """The proxy reads the record, and its Identity in the policy namespace, live on each request; a
    missing grant fails closed."""
    settings = config_map_yaml(resources, "egress-go-settings", "settings.yaml")
    allowed = set(settings["allowed_service_account_namespaces"])
    policy_namespace = settings["rules_namespace"]
    identities = {(d["metadata"]["namespace"], d["metadata"]["name"]) for d in resources if d["kind"] == "Identity"}
    assert NAMESPACE in allowed
    permissions = Rbac(resources).identity("ServiceAccount", "egress", staging.ENV.namespace)
    for record in _standing_records(resources):
        identity = record["spec"]["subject"]["identity"]["name"]
        assert (policy_namespace, identity) in identities
        assert not uncovered(
            [
                Permission(NAMESPACE, GROUP, "workloadcredentials", "get", record["metadata"]["name"]),
                Permission(policy_namespace, GROUP, "identities", "get", identity),
            ],
            permissions,
        )


if __name__ == "__main__":
    pytest_bazel.main()
