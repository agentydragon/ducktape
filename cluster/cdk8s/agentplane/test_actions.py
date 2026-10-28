"""The MCP authority's public HTTPRoute exposes protocol paths and nothing private."""

from __future__ import annotations

import json
from typing import Any

import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane import staging

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


def test_public_route_exposes_protocol_paths_only(staging_manifests: list[dict[str, Any]]) -> None:
    route = one(
        doc for doc in staging_manifests if doc["kind"] == "HTTPRoute" and doc["metadata"]["name"] == "mcp-edge-mcp"
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


def _gaffer_approval(manifests: list[dict[str, Any]], action: str) -> dict[str, Any]:
    policy = one(
        doc
        for doc in manifests
        if doc["kind"] == "PolicySet" and doc["metadata"]["name"] == "gaffer-private-branch-and-pr-creation"
    )
    return one(rule["policy"] for rule in policy["spec"]["rules"] if rule["policy"]["actions"] == {"github": [action]})


def test_gaffer_branch_policy_approves_only_create_branch(staging_manifests: list[dict[str, Any]]) -> None:
    approval = _gaffer_approval(staging_manifests, "create_branch")
    assert approval["type"] == "argument_schema"
    assert approval["schema"]["properties"]["owner"] == {"const": "agentydragon"}
    assert approval["schema"]["properties"]["repo"] == {"const": "gaffer-private"}
    assert approval["schema"]["required"] == ["owner", "repo", "branch"]
    assert approval["schema"]["additionalProperties"] is False


def test_gaffer_pr_policy_approves_only_create_pull_request(staging_manifests: list[dict[str, Any]]) -> None:
    approval = _gaffer_approval(staging_manifests, "create_pull_request")
    assert approval["type"] == "argument_schema"
    assert approval["schema"]["properties"]["owner"] == {"const": "agentydragon"}
    assert approval["schema"]["properties"]["repo"] == {"const": "gaffer-private"}
    assert approval["schema"]["required"] == ["owner", "repo", "title", "head", "base"]
    assert approval["schema"]["properties"]["maintainer_can_modify"] == {"const": False}
    assert "reviewers" not in approval["schema"]["properties"]
    assert approval["schema"]["additionalProperties"] is False


def test_cross_owner_reader_is_explicit_and_json_encoded(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "actions"
    )
    container = one(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "actions")
    readers = one(item for item in container["env"] if item["name"] == "AGENTPLANE_ACTIONS_READER_ACCOUNTS")
    assert json.loads(readers["value"]) == [{"namespace": staging.ENV.namespace, "name": "notifications"}]
    assert not any(arg.startswith("--reader-accounts") for arg in container.get("args", []))


def test_actions_roll_does_not_overlap_execution_protocols(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "actions"
    )
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}


def test_actions_migrate_from_service_image(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "actions"
    )
    pod = deployment["spec"]["template"]["spec"]
    migrator = one(item for item in pod["initContainers"] if item["name"] == "migrate")
    server = one(item for item in pod["containers"] if item["name"] == "actions")
    assert migrator["image"] == server["image"]
    assert migrator["command"] == ["/usr/local/bin/actions-migrate"]


if __name__ == "__main__":
    pytest_bazel.main()
