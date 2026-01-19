"""Unit tests for MCP Starter Template server."""

import pytest_bazel
from fastmcp.client import Client as FastMCPClient

from adgn_mcp_starter.server import create_mcp_server


def test_server_creation() -> None:
    assert create_mcp_server() is not None


async def test_greet_tool(mcp_client: FastMCPClient) -> None:
    result = await mcp_client.call_tool(name="greet", arguments={"name": "Alice"})
    assert not getattr(result, "is_error", False)


async def test_get_text_chunks_tool(mcp_client: FastMCPClient) -> None:
    result = await mcp_client.call_tool(name="get_text_chunks", arguments={"text": "Hello World", "chunk_size": 5})
    assert not getattr(result, "is_error", False)
    sc = result.structured_content
    assert isinstance(sc, dict)
    chunks = sc["result"]
    assert len(chunks) == 3
    assert chunks[0]["content"] == "Hello"
    assert chunks[2]["is_final"] is True


async def test_get_text_chunks_validation_bad(mcp_client: FastMCPClient) -> None:
    bad = await mcp_client.call_tool(
        name="get_text_chunks", arguments={"text": "x", "chunk_size": 0}, raise_on_error=False
    )
    assert getattr(bad, "is_error", False)


async def test_get_text_chunks_validation_big(mcp_client: FastMCPClient) -> None:
    big = await mcp_client.call_tool(
        name="get_text_chunks", arguments={"text": "x", "chunk_size": 20000}, raise_on_error=False
    )
    assert getattr(big, "is_error", False)


async def test_generate_sample_image_tool(mcp_client: FastMCPClient) -> None:
    result = await mcp_client.call_tool(name="generate_sample_image", arguments={})
    assert not getattr(result, "is_error", False)
    contents = result.content or []
    img = next((c for c in contents if getattr(c, "type", None) == "image"), None)
    assert img is not None
    assert getattr(img, "mimeType", None) == "image/png"
    assert len(getattr(img, "data", "")) > 0


if __name__ == "__main__":
    pytest_bazel.main()
