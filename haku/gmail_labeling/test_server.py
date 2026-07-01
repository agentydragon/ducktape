import pytest
import pytest_bazel
from fastmcp import Client
from fastmcp.exceptions import ToolError

from haku.gmail_labeling.server import build_mcp


async def test_tool_surface(client):
    async with Client(build_mcp(client)) as mcp_client:
        tools = {tool.name for tool in await mcp_client.list_tools()}
    assert tools == {"list_labels", "modify_labels", "create_label", "rename_label", "delete_label"}


async def test_modify_labels_tool_succeeds(client, backend):
    async with Client(build_mcp(client)) as mcp_client:
        result = await mcp_client.call_tool("modify_labels", {"thread_ids": ["t1"], "add": ["haku/triaged"]})
    assert not result.is_error
    assert len(backend.thread_mods) == 1
    assert backend.thread_mods[0][0] == ["t1"]


async def test_modify_labels_tool_batches_multiple_threads(client, backend):
    async with Client(build_mcp(client)) as mcp_client:
        result = await mcp_client.call_tool(
            "modify_labels", {"thread_ids": ["t1", "t2", "t3"], "add": ["haku/triaged"]}
        )
    assert not result.is_error
    assert backend.thread_mods == [(["t1", "t2", "t3"], [backend.list_labels()[0].id], [])]


async def test_modify_labels_tool_rejects_outside_prefix(client):
    async with Client(build_mcp(client)) as mcp_client:
        with pytest.raises(ToolError):
            await mcp_client.call_tool("modify_labels", {"thread_ids": ["t1"], "add": ["Important"]})


if __name__ == "__main__":
    pytest_bazel.main()
