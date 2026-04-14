"""Dev tool: dump all MCP tool definitions to stdout as JSON.

Usage:
    bb run //x/grocy_mcp:dump_tools
"""

from __future__ import annotations

import asyncio
import json

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


async def _main() -> None:
    mcp = build_mcp(_settings())
    tools = await mcp.list_tools()
    all_tools = [tool.to_mcp_tool().model_dump(mode="json") for tool in tools]
    print(json.dumps(all_tools, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
