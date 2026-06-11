"""Discover MCP server instructions and available tools.

Run this script to see what the MCP server provides:
    python -m agent_pkg.runtime.examples.mcp_discover
"""

import asyncio
import json

from agent_pkg.runtime.mcp import mcp_client_from_env


async def main() -> None:
    async with mcp_client_from_env() as (client, init_result):
        # Print server instructions
        print("## Server Instructions\n")
        print("<mcp-server-instructions>")
        print(init_result.instructions or "(No instructions provided)")
        print("</mcp-server-instructions>\n")

        # List all available tools with schemas
        print("## Available Tools\n")
        tools = await client.list_tools()
        for tool in tools:
            print(f"### {tool.name}")
            if tool.description:
                print(f"\n{tool.description}\n")
            print(f"\n**Input schema:**\n```json\n{json.dumps(tool.inputSchema, indent=2)}\n```\n")

        # Example: calling a tool (commented out)
        # result = await client.call_tool("tool_name", {"arg": "value"})
        # print(result.content)


if __name__ == "__main__":
    asyncio.run(main())
