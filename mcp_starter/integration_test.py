"""
Integration test for MCP Starter Template server.
Tests the actual server functionality without requiring Claude SDK.
"""

import mcp.types as mcp_types
import pytest
import pytest_bazel
from fastmcp import Client
from fastmcp.exceptions import ToolError


async def test_list_tools_includes_expected(mcp_client: Client) -> None:
    tools = await mcp_client.list_tools()
    tool_names = {t.name for t in tools}
    assert {"greet", "get_text_chunks", "generate_sample_image"} <= tool_names


async def test_content_chunks_via_client(mcp_client: Client) -> None:
    """Test content chunking via the public MCP interface."""
    result = await mcp_client.call_tool(name="get_text_chunks", arguments={"text": "Hello World", "chunk_size": 5})
    assert not result.is_error
    # The server returns structured content as {"result": [ContentChunk, ...]}
    sc = result.structured_content
    assert isinstance(sc, dict)
    chunks = sc["result"]
    assert isinstance(chunks, list), f"Unexpected chunks type: {type(chunks)}"
    assert len(chunks) == 3
    assert chunks[0]["content"] == "Hello"
    assert chunks[1]["content"] == " Worl"
    assert chunks[2]["content"] == "d"
    assert chunks[2]["is_final"] is True


async def test_greeting_via_client(mcp_client: Client) -> None:
    """Test greeting via the public MCP interface."""
    result = await mcp_client.call_tool(name="greet", arguments={"name": "Bob"})
    assert not result.is_error


async def test_image_generation_via_client(mcp_client: Client) -> None:
    """Test image generation via the public MCP interface."""
    result = await mcp_client.call_tool(name="generate_sample_image", arguments={})
    assert not result.is_error
    # Expect an ImageContent in content list
    contents = result.content or []
    assert contents, "Expected image content from server"
    img = next((c for c in contents if isinstance(c, mcp_types.ImageContent)), None)
    assert isinstance(img, mcp_types.ImageContent), "No image content block returned"
    assert img.mimeType == "image/png"
    assert isinstance(img.data, str)
    assert len(img.data) > 0


async def test_unknown_tool_raises(mcp_client: Client) -> None:
    """Unknown tool calls should raise ToolError by default."""
    with pytest.raises(ToolError):
        await mcp_client.call_tool(name="__does_not_exist__", arguments={})


if __name__ == "__main__":
    pytest_bazel.main()
