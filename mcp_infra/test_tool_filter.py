from __future__ import annotations

import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.exceptions import ToolError

from mcp_infra.tool_filter import ToolFilter, ToolFilterMiddleware

_READ_ONLY = ToolFilter(allow=["read_*", "search_nodes"])


def _server(policy: ToolFilter) -> FastMCP:
    mcp = FastMCP(name="filter-test")

    @mcp.tool
    def search_nodes() -> str:
        return "read"

    @mcp.tool
    def read_node() -> str:
        return "read"

    @mcp.tool
    def trash_node() -> str:
        return "write"

    mcp.add_middleware(ToolFilterMiddleware(policy))
    return mcp


def test_admits_allowlist_is_default_deny() -> None:
    assert _READ_ONLY.admits("read_node")
    assert _READ_ONLY.admits("search_nodes")
    assert not _READ_ONLY.admits("trash_node")
    assert not _READ_ONLY.admits("get_or_create_calendar_node")


def test_admits_deny_subtracts_from_open_gate() -> None:
    policy = ToolFilter(deny=["*_node"])
    assert not policy.admits("read_node")
    assert policy.admits("list_tags")


def test_admits_deny_overrides_allow() -> None:
    policy = ToolFilter(allow=["read_*"], deny=["read_secret"])
    assert policy.admits("read_node")
    assert not policy.admits("read_secret")


def test_admits_globs_are_case_sensitive() -> None:
    assert not ToolFilter(allow=["read_*"]).admits("READ_node")


def test_empty_policy_admits_everything() -> None:
    assert ToolFilter().admits("trash_node")


async def test_list_tools_hides_disallowed() -> None:
    async with Client(_server(_READ_ONLY)) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"read_node", "search_nodes"}


async def test_call_allowed_tool_succeeds() -> None:
    async with Client(_server(_READ_ONLY)) as client:
        result = await client.call_tool("search_nodes", {})
    assert result.content[0].text == "read"


async def test_call_disallowed_tool_is_rejected() -> None:
    # A hidden tool must still be unreachable by name, not just absent from the list.
    async with Client(_server(_READ_ONLY)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("trash_node", {})


if __name__ == "__main__":
    pytest_bazel.main()
