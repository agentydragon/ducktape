"""Dev tool: dump all MCP tool definitions to stdout as JSON.

Usage:
    bb run //x/grocy_mcp:dump_tools
"""

from __future__ import annotations

import asyncio
import json

import httpx

from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.server import build_mcp


async def _main() -> None:
    settings = ServerSettings(grocy_url="https://grocy.example.com")
    async with httpx.AsyncClient(base_url=f"{settings.grocy_url}/api") as client:
        mcp = build_mcp(settings, client=client)
        tools = await mcp.list_tools()
    all_tools = [tool.to_mcp_tool().model_dump(mode="json") for tool in tools]
    print(json.dumps(all_tools, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
