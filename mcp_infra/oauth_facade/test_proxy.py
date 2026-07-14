from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.exceptions import ToolError

from airlock.conftest import as_remote_server
from mcp_infra.authentik_auth.config import AuthentikAuthConfig
from mcp_infra.oauth_facade.config import FacadeSettings, HttpUpstream
from mcp_infra.oauth_facade.proxy import build_proxy_server
from mcp_infra.tool_filter import ToolFilter, ToolFilterMiddleware


def _settings(downstream_url: str) -> FacadeSettings:
    return FacadeSettings(
        auth=AuthentikAuthConfig(
            oidc_issuer="https://auth.example.com/application/o/test/",
            oidc_client_id="id",
            oidc_client_secret="secret",
            public_base_url="https://test.example.com",
        ),
        upstream=HttpUpstream(url=downstream_url, bearer_token="server-pat"),
        facade_name="Test Facade",
    )


@asynccontextmanager
async def _facade_client(settings: FacadeSettings):
    facade = build_proxy_server(settings)
    async with Client(facade) as client:
        yield client


async def test_facade_mirrors_downstream_tools() -> None:
    downstream = FastMCP("downstream")

    @downstream.tool
    async def echo(text: str) -> str:
        return f"echoed: {text}"

    async with as_remote_server(downstream) as remote:
        settings = _settings(remote.url)
        async with _facade_client(settings) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools] == ["echo"]


async def test_facade_forwards_tool_calls() -> None:
    downstream = FastMCP("downstream")

    @downstream.tool
    async def echo(text: str) -> str:
        return f"echoed: {text}"

    async with as_remote_server(downstream) as remote:
        settings = _settings(remote.url)
        async with _facade_client(settings) as client:
            result = await client.call_tool_mcp("echo", {"text": "hi"})
        assert result.isError is False
        assert result.content[0].text == "echoed: hi"


async def test_facade_filters_proxied_tools() -> None:
    downstream = FastMCP("downstream")

    @downstream.tool
    async def read_node() -> str:
        return "r"

    @downstream.tool
    async def trash_node() -> str:
        return "w"

    async with as_remote_server(downstream) as remote:
        facade = build_proxy_server(_settings(remote.url))
        facade.add_middleware(ToolFilterMiddleware(ToolFilter(allow={"read_node"})))
        async with Client(facade) as client:
            assert [tool.name for tool in await client.list_tools()] == ["read_node"]
            with pytest.raises(ToolError):
                await client.call_tool("trash_node", {})


if __name__ == "__main__":
    pytest_bazel.main()
