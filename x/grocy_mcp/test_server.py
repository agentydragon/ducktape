"""Smoke tests for the OpenAPI → FastMCP wiring.

- `build_mcp` parses the checked-in Grocy spec and constructs a FastMCP
  instance without raising. Catches spec drift when
  <grocy_openapi.json> is refreshed.
- `_strip_empty_enums` removes the one known Grocy 4.6.0 spec quirk
  (`ExposedEntityEditRequiresAdmin`), which would otherwise cause
  FastMCP's pydantic parser to reject the whole document.

FastMCP doesn't expose a public synchronous API to enumerate generated
tools (`get_tool(name)` is singular, the list method is async). We keep
the smoke test narrow rather than reach into private attributes.
"""

from __future__ import annotations

import pytest_bazel

from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.server import _strip_empty_enums, build_mcp


def _settings() -> ServerSettings:
    return ServerSettings(
        oidc_issuer="https://auth.example.com/application/o/grocy-mcp/",
        oidc_client_id="id",
        oidc_client_secret="secret",
        public_base_url="https://grocy-mcp.example.com",
        grocy_url="https://grocy.example.com",
        grocy_proxy_client_id="grocy-proxy-id",
    )


def test_build_mcp_accepts_grocy_spec() -> None:
    # No assertions beyond "construction doesn't raise". FastMCP's
    # pydantic validator will reject the whole spec if anything is
    # structurally wrong — that's a failure the next time we refresh
    # grocy_openapi.json, and this test catches it.
    build_mcp(_settings())


def test_strip_empty_enums_removes_empty_enum() -> None:
    spec = {
        "components": {"schemas": {"Bad": {"type": "string", "enum": []}, "Good": {"type": "string", "enum": ["ok"]}}}
    }
    _strip_empty_enums(spec)
    assert "enum" not in spec["components"]["schemas"]["Bad"]
    assert spec["components"]["schemas"]["Good"]["enum"] == ["ok"]


if __name__ == "__main__":
    pytest_bazel.main()
