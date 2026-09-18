"""The workload EgressPolicy grants agents the Actions API's agent-facing surface only."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

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


if __name__ == "__main__":
    pytest_bazel.main()
