from __future__ import annotations

from fastmcp.client import Client
from fastmcp.server import FastMCP


async def test_server_slot_spec_open_initializes_once() -> None:
    app = FastMCP("demo")

    @app.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers"""
        return a + b

    async with Client(app) as client:
        init = client.initialize_result
        assert isinstance(init.protocolVersion, str)
        tools = await client.list_tools()
        assert any(t.name == "add" for t in tools), tools
