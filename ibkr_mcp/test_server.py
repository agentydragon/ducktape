"""Reflection smoke test: `build_mcp` turns IBKR's spec into exactly the
read-only tool surface, and a generated tool routes to the gateway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_bazel
import respx
from fastmcp import Client, FastMCP
from prometheus_client import REGISTRY

from ibkr_mcp.mcp_types import ServerSettings
from ibkr_mcp.route_policy import READ_ONLY_OPERATIONS
from ibkr_mcp.server import build_mcp, record_tool_count

_EXPECTED_NAMES = {spec.name for spec in READ_ONLY_OPERATIONS.values()}
_GATEWAY = "https://gw.test/v1/api"


@pytest.fixture
async def mcp() -> AsyncIterator[FastMCP]:
    async with httpx.AsyncClient(base_url=_GATEWAY) as client:
        yield build_mcp(ServerSettings(), client=client)


async def test_reflected_tools_are_exactly_the_allowlist(mcp: FastMCP) -> None:
    assert {tool.name for tool in await mcp.list_tools()} == _EXPECTED_NAMES


async def test_no_tool_looks_like_order_placement(mcp: FastMCP) -> None:
    for tool in await mcp.list_tools():
        assert "order" not in tool.name.lower()


async def test_tool_count_metric_is_exported(mcp: FastMCP) -> None:
    tool_count = await record_tool_count(mcp)
    assert tool_count == len(_EXPECTED_NAMES)
    assert REGISTRY.get_sample_value("ibkr_mcp_tools") == tool_count


@respx.mock
async def test_snapshot_tool_routes_to_gateway(mcp: FastMCP) -> None:
    route = respx.get(f"{_GATEWAY}/iserver/marketdata/snapshot").mock(
        return_value=httpx.Response(200, json=[{"conid": 265598, "31": "150.00"}])
    )
    async with Client(mcp) as client:
        result = await client.call_tool_mcp("market_data_snapshot", {"conids": "265598", "fields": "31"})

    assert result.isError is False
    assert route.called
    assert route.calls.last.request.url.params["conids"] == "265598"


if __name__ == "__main__":
    pytest_bazel.main()
