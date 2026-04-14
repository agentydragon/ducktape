"""Dump all MCP tool definitions to undeclared test outputs."""

from __future__ import annotations

import json

import pytest_bazel

from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.server import build_mcp


def _settings() -> ServerSettings:
    return ServerSettings(
        oidc_issuer="https://auth.example.com/application/o/grocy-mcp/",
        oidc_client_id="id",
        oidc_client_secret="secret",
        public_base_url="https://grocy-mcp.example.com",
        grocy_url="https://grocy.example.com",
        grocy_proxy_client_id="grocy-proxy-id",
    )


async def test_dump_all_tools() -> None:
    mcp = build_mcp(_settings())
    tools = await mcp.list_tools()
    all_tools = [tool.to_mcp_tool().model_dump(mode="json") for tool in tools]
    out = undeclared_outputs_dir() / "all_tools.json"
    out.write_text(json.dumps(all_tools, indent=2))
    print(f"Wrote {len(all_tools)} tools to {out}")
    for tool in all_tools:
        print(f"  {tool['name']}")


if __name__ == "__main__":
    pytest_bazel.main()
