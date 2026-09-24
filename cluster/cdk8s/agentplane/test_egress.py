"""The workload EgressPolicy grants agents the Actions API's agent-facing surface only."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane import testing
from cluster.cdk8s.agentplane.app_settings import (
    BASIC_POLICY,
    FORGEJO_HAKU_POLICY,
    GITHUB_PUBLIC_POLICY,
    GOOGLE_READONLY_POLICY,
    GROCY_SF_READONLY_POLICY,
    HOME_ASSISTANT_READONLY_POLICY,
)
from cluster.cdk8s.agentplane.conftest import NAMESPACES

# What a workload token may reach on the Actions service: the MCP endpoint, its schema and
# the action-group/request API. The operator API (/v1/operator/*) and the OAuth endpoints
# (/register, /token, ...) stay off this policy.
_AGENT_FACING_PREFIXES = ("/mcp", "/openapi.json", "/v1/action-")


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


def test_testing_github_policy_has_its_credential_and_no_real_account_credentials(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    manifests = agentplane_manifests[testing.ENV.namespace]
    github = one(
        doc for doc in manifests if doc["kind"] == "EgressPolicy" and doc["metadata"]["name"] == GITHUB_PUBLIC_POLICY
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
        in {FORGEJO_HAKU_POLICY, GOOGLE_READONLY_POLICY, GROCY_SF_READONLY_POLICY, HOME_ASSISTANT_READONLY_POLICY}
        for doc in manifests
    )


def test_upstream_bundles_have_independent_environment_ownership(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    names = set()
    for namespace, manifests in agentplane_manifests.items():
        bundle = one(
            doc
            for doc in manifests
            if doc["kind"] == "Bundle" and doc["metadata"]["name"] == f"{namespace}-egress-upstream-ca"
        )
        name = bundle["metadata"]["name"]
        assert name not in names, "cluster-scoped upstream Bundles must have separate owners"
        names.add(name)
        assert bundle["spec"]["target"]["namespaceSelector"] == {
            "matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": [namespace]}]
        }
        assert bundle["spec"]["sources"] == [
            {"useDefaultCAs": True},
            {"configMap": {"name": "kube-root-ca.crt", "key": "ca.crt"}},
        ]


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


if __name__ == "__main__":
    pytest_bazel.main()
