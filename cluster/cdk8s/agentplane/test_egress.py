"""The workload EgressPolicy grants agents the Actions API's agent-facing surface only."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import testing
from cluster.cdk8s.agentplane.app_settings import BASIC_POLICY
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


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_egress_presets_and_rules_reference_available_resources(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    manifests = agentplane_manifests[namespace]
    credentials = {doc["metadata"]["name"] for doc in manifests if doc["kind"] == "EgressCredential"}
    policies = {doc["metadata"]["name"] for doc in manifests if doc["kind"] == "EgressPolicy"}
    for doc in manifests:
        if doc["kind"] == "EgressPolicy":
            for rule in doc["spec"]["rules"]:
                if "credentialRef" in rule:
                    assert rule["credentialRef"]["name"] in credentials
    app = one(
        doc for doc in manifests if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-app-config"
    )
    config = yaml.safe_load(app["data"]["config.yaml"])
    for preset in config["sandbox_presets"].values():
        assert set(preset["policies"]) <= policies


def test_testing_egress_does_not_watch_or_reference_static_secrets(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    manifests = agentplane_manifests[testing.ENV.namespace]
    settings = one(
        doc
        for doc in manifests
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-egress-settings"
    )
    assert yaml.safe_load(settings["data"]["settings.yaml"]).get("credentials_namespace") is None
    credentials = [doc for doc in manifests if doc["kind"] == "EgressCredential"]
    assert credentials
    assert all("secretRef" not in credential["spec"]["source"] for credential in credentials)


if __name__ == "__main__":
    pytest_bazel.main()
