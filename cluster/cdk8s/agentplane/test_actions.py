"""The Actions service's public HTTPRoute exposes MCP/OAuth protocol paths and nothing private."""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest
import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane.conftest import NAMESPACES

# types-jsonschema stubs import referencing; the mypy aspect needs that typed package directly.
# gazelle:include_dep @pypi//referencing

# REST, docs, health and consent live behind the Action API's own authentication, never on
# the public MCP origin.
_PRIVATE_PREFIXES = ("/v1", "/docs", "/openapi", "/health", "/consent")
# Published FastMCP/MCP protocol endpoints, not a copy of the route's whole roster.
_MCP_PROTOCOL_PATHS = {
    "/mcp",
    "/register",
    "/authorize",
    "/token",
    "/revoke",
    "/auth/callback",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource/mcp",
}


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_public_route_exposes_protocol_paths_only(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    route = one(
        doc
        for doc in agentplane_manifests[namespace]
        if doc["kind"] == "HTTPRoute" and doc["metadata"]["name"] == "actions-mcp"
    )
    paths = set()
    for rule in route["spec"]["rules"]:
        for match in rule["matches"]:
            assert match["path"]["type"] == "Exact"
            paths.add(match["path"]["value"])
        # No rewrite may turn an allowed protocol path into a private API request.
        assert all(filter_["type"] == "ResponseHeaderModifier" for filter_ in rule.get("filters", []))
    assert not any(path == "/" or path.startswith(_PRIVATE_PREFIXES) for path in paths)
    assert paths >= _MCP_PROTOCOL_PATHS


def _gaffer_approval(manifests: dict[str, list[dict[str, Any]]], action: str) -> dict[str, Any]:
    policy = one(
        doc
        for doc in manifests["agentplane-staging"]
        if doc["kind"] == "PolicySet" and doc["metadata"]["name"] == "gaffer-private-branch-and-pr-creation"
    )
    return one(rule["policy"] for rule in policy["spec"]["rules"] if rule["policy"]["actions"] == {"github": [action]})


def test_gaffer_branch_policy_approves_only_create_branch(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    approval = _gaffer_approval(agentplane_manifests, "create_branch")
    assert approval["type"] == "argument_schema"
    assert approval["schema"]["properties"]["owner"] == {"const": "agentydragon"}
    assert approval["schema"]["properties"]["repo"] == {"const": "gaffer-private"}
    assert approval["schema"]["required"] == ["owner", "repo", "branch"]
    assert approval["schema"]["additionalProperties"] is False


def test_gaffer_pr_policy_approves_only_create_pull_request(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    approval = _gaffer_approval(agentplane_manifests, "create_pull_request")
    assert approval["type"] == "argument_schema"
    assert approval["schema"]["properties"]["owner"] == {"const": "agentydragon"}
    assert approval["schema"]["properties"]["repo"] == {"const": "gaffer-private"}
    assert approval["schema"]["required"] == ["owner", "repo", "title", "head", "base"]
    assert approval["schema"]["properties"]["maintainer_can_modify"] == {"const": False}
    assert "reviewers" not in approval["schema"]["properties"]
    assert approval["schema"]["additionalProperties"] is False


def _kubernetes_inspection_approves(
    manifests: dict[str, list[dict[str, Any]]], action: str, arguments: dict[str, Any]
) -> bool:
    """Whether a `kubernetes-inspection-reads` rule approves the call, judged the way the Action
    Service judges `exact_actions` and `argument_schema` rules."""
    policy_set = one(
        doc
        for doc in manifests["agentplane-staging"]
        if doc["kind"] == "PolicySet" and doc["metadata"]["name"] == "kubernetes-inspection-reads"
    )
    for rule in policy_set["spec"]["rules"]:
        policy = rule["policy"]
        if action not in policy["actions"].get("kubernetes_admin", []):
            continue
        if policy["type"] == "exact_actions":
            return True
        assert policy["type"] == "argument_schema"
        schema = policy["schema"]
        if jsonschema.validators.validator_for(schema)(schema).is_valid(arguments):
            return True
    return False


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        ("nodes_stats_summary", {"name": "fake-node"}),
        ("pods_log", {"name": "fake-pod", "namespace": "fake-ns"}),
        ("nodes_log", {"name": "fake-node", "query": "kubelet"}),
        ("resources_get", {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "name": "fake-db"}),
        ("resources_list", {"apiVersion": "v1", "kind": "ConfigMap", "namespace": "fake-ns"}),
    ],
)
def test_kubernetes_inspection_reads_approve_reads(
    agentplane_manifests: dict[str, list[dict[str, Any]]], action: str, arguments: dict[str, Any]
) -> None:
    assert _kubernetes_inspection_approves(agentplane_manifests, action, arguments)


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        ("resources_get", {"apiVersion": "v1", "kind": "Secret", "name": "fake-secret"}),
        ("resources_list", {"apiVersion": "v1", "kind": "secrets"}),
        ("resources_list", {"apiVersion": "v1", "kind": "SecretList"}),
        ("resources_get", {"apiVersion": "v1", "kind": " Secret", "name": "fake-secret"}),
        ("nodes_log", {"name": "fake-node", "query": "/var/log/audit/kube/kube-apiserver.log"}),
        ("configuration_view", {"minified": False}),
        ("pods_exec", {"name": "fake-pod", "command": ["true"]}),
        ("pods_run", {"image": "fake-image"}),
        ("resources_delete", {"apiVersion": "v1", "kind": "Pod", "name": "fake-pod"}),
    ],
)
def test_kubernetes_inspection_reads_leave_secrets_exec_and_writes_manual(
    agentplane_manifests: dict[str, list[dict[str, Any]]], action: str, arguments: dict[str, Any]
) -> None:
    assert not _kubernetes_inspection_approves(agentplane_manifests, action, arguments)


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_cross_owner_reader_is_explicit_and_json_encoded(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    deployment = one(
        doc
        for doc in agentplane_manifests[namespace]
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "actions"
    )
    container = one(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "actions")
    readers = one(item for item in container["env"] if item["name"] == "AGENTPLANE_ACTIONS_READER_ACCOUNTS")
    assert json.loads(readers["value"]) == [{"namespace": namespace, "name": "notifications"}]
    assert not any(arg.startswith("--reader-accounts") for arg in container["args"])


if __name__ == "__main__":
    pytest_bazel.main()
