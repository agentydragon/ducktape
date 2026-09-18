"""The Actions service's public HTTPRoute exposes MCP/OAuth protocol paths and nothing private."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane.conftest import NAMESPACES

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
        if doc["kind"] == "HTTPRoute" and doc["metadata"]["name"] == "agentplane-actions-mcp"
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


if __name__ == "__main__":
    pytest_bazel.main()
