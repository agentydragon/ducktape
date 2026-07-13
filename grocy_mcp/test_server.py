"""Smoke test: `build_mcp` parses the fixed Grocy spec without raising."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_bazel
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from prometheus_client import REGISTRY

from grocy_mcp.batch_tools import build_batch_tools_mcp
from grocy_mcp.client import GrocyClient
from grocy_mcp.mcp_types import ServerSettings
from grocy_mcp.server import build_mcp, record_tool_count
from mcp_infra.request_scoped_openapi import borrowed_http_client_provider


@pytest.fixture
async def plain_mcp() -> AsyncIterator[FastMCP]:
    settings = ServerSettings(grocy_url="https://grocy.example.com")
    async with GrocyClient(base_url=f"{settings.grocy_url}/api") as client:
        yield build_mcp(settings, client_provider=borrowed_http_client_provider(client))


async def test_build_mcp_accepts_grocy_spec(plain_mcp: FastMCP) -> None:
    assert await plain_mcp.list_tools()


async def test_record_tool_count_exports_metric(plain_mcp: FastMCP) -> None:
    tool_count = await record_tool_count(plain_mcp)

    assert tool_count > 0
    assert REGISTRY.get_sample_value("grocy_mcp_tools") == tool_count


async def test_batch_client_dependency_failure_is_mcp_error_before_body() -> None:
    dependency_entries = 0

    class RejectedClient:
        async def __aenter__(self) -> GrocyClient:
            nonlocal dependency_entries
            dependency_entries += 1
            raise ToolError("Backend authentication failed")

        async def __aexit__(self, *_: object) -> None:
            pass

    def rejected_client() -> RejectedClient:
        return RejectedClient()

    settings = ServerSettings(grocy_url="https://grocy.example.com")
    mcp = build_mcp(settings, client_provider=rejected_client)
    async with Client(mcp) as mcp_client:
        result = await mcp_client.call_tool_mcp("entities_list", {"entity_types": ["products"]})

    assert result.isError is True
    assert "Backend authentication failed" in "\n".join(
        block.text for block in result.content if hasattr(block, "text")
    )
    assert dependency_entries == 1


async def test_batch_client_dependency_is_hidden_and_lives_for_one_call() -> None:
    entered: list[GrocyClient] = []
    exited: list[GrocyClient] = []
    backend_requests: list[str] = []

    async def backend(request: httpx.Request) -> httpx.Response:
        backend_requests.append(request.url.path)
        return httpx.Response(200, json=[])

    @asynccontextmanager
    async def per_call_client() -> AsyncIterator[GrocyClient]:
        async with GrocyClient(
            base_url="https://grocy.example.com/api", transport=httpx.MockTransport(backend)
        ) as client:
            entered.append(client)
            try:
                yield client
            finally:
                exited.append(client)

    settings = ServerSettings(grocy_url="https://grocy.example.com")
    injected = build_batch_tools_mcp(settings, client_provider=per_call_client)
    injected_schemas = {tool.name: tool.parameters for tool in await injected.list_tools()}
    async with Client(injected) as mcp_client:
        result = await mcp_client.call_tool_mcp("entities_list", {"entity_types": ["products", "locations"]})

    assert all("client" not in schema.get("properties", {}) for schema in injected_schemas.values())
    assert result.isError is False
    assert sorted(backend_requests) == ["/api/objects/locations", "/api/objects/products"]
    assert len(entered) == 1
    assert exited == entered
    assert entered[0].is_closed


if __name__ == "__main__":
    pytest_bazel.main()
