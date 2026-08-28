"""Dev tool: dump all MCP tool definitions to stdout as JSON.

Usage:
    bb run //grocy_mcp:dump_tools_bin
"""

from __future__ import annotations

import asyncio
import json

from grocy_mcp.client import GrocyClient
from grocy_mcp.mcp_types import ServerSettings
from grocy_mcp.server import build_mcp
from mcp_infra.request_scoped_openapi import borrowed_http_client_provider


async def _main() -> None:
    settings = ServerSettings(grocy_url="https://grocy.example.com")
    async with GrocyClient(base_url=f"{settings.grocy_url}/api") as client:
        mcp = build_mcp(settings, client_provider=borrowed_http_client_provider(client))
        tools = await mcp.list_tools()
    all_tools = [tool.to_mcp_tool().model_dump(mode="json") for tool in tools]
    print(json.dumps(all_tools, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
