"""Tests for block-level resource reading with truncation markers."""

import pytest
import pytest_bazel

from mcp_infra.compositor.resources_server import ReadBlocksArgs
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.prefix import MCPMountPrefix


@pytest.mark.asyncio
async def test_read_blocks_single_text_block_full(compositor, typed_resources_client):
    """Test reading a single text block that fits within max_bytes."""
    # Setup: origin server with a small text resource
    origin = EnhancedFastMCP("origin")

    @origin.resource("resource://test.txt", name="test", mime_type="text/plain")
    async def test_txt() -> str:
        return "Hello, World!"

    await compositor.mount_inproc(MCPMountPrefix("origin"), origin)

    # Act: read with default max_bytes (100KB, way more than needed)
    result = await typed_resources_client.read_blocks(
        ReadBlocksArgs(server="origin", uri="resource://test.txt", start_block=0, start_offset=0, max_bytes=100 * 1024)
    )

    # Assert: full content returned, no truncation
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.text == "Hello, World!"


@pytest.mark.asyncio
async def test_read_blocks_truncate_at_end(compositor, typed_resources_client):
    """Test truncating a text block that exceeds max_bytes."""
    # Setup: text block with 100 bytes
    long_text = "a" * 100
    origin = EnhancedFastMCP("origin")

    @origin.resource("resource://long.txt", name="long", mime_type="text/plain")
    async def long_txt() -> str:
        return long_text

    await compositor.mount_inproc(MCPMountPrefix("origin"), origin)

    # Act: read with max_bytes=50
    result = await typed_resources_client.read_blocks(
        ReadBlocksArgs(server="origin", uri="resource://long.txt", start_block=0, start_offset=0, max_bytes=50)
    )

    # Assert: partial content with truncation marker
    assert len(result.blocks) == 1

    truncated = result.blocks[0]
    assert truncated.kind == "truncated"
    assert truncated.block_index == 0
    assert truncated.started_at == 0
    assert truncated.ended_at == 50
    assert truncated.full_size == 100
    assert truncated.content.text == "a" * 50


@pytest.mark.asyncio
async def test_read_blocks_truncate_within_single_large_block(compositor, typed_resources_client):
    """Test truncating a large block - the truncated marker contains partial content."""
    # Setup: single large block (FastMCP resources are always single-block)
    origin = EnhancedFastMCP("origin")

    @origin.resource("resource://large.txt", name="large", mime_type="text/plain")
    async def large_txt() -> str:
        # Create a 100-byte block
        return "x" * 100

    await compositor.mount_inproc(MCPMountPrefix("origin"), origin)

    # Act: read with max_bytes=40 (partial block)
    result = await typed_resources_client.read_blocks(
        ReadBlocksArgs(server="origin", uri="resource://large.txt", start_block=0, start_offset=0, max_bytes=40)
    )

    # Assert: single truncation marker with partial content
    assert len(result.blocks) == 1

    truncated = result.blocks[0]
    assert truncated.kind == "truncated"
    assert truncated.block_index == 0
    assert truncated.started_at == 0
    assert truncated.ended_at == 40
    assert truncated.full_size == 100
    assert truncated.content.text == "x" * 40


if __name__ == "__main__":
    pytest_bazel.main()
