"""Main MCP server implementation with simple greeting functionality."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from mcp.types import ImageContent
from pydantic import BaseModel


class ContentChunk(BaseModel):
    """A chunk of content for streaming responses."""

    chunk_id: int
    content: str
    is_final: bool = False


def create_mcp_server(debug_mcp: bool = False) -> FastMCP[Any]:
    """Create the MCP server with greeting functionality."""
    mcp: FastMCP[Any] = FastMCP("MCP Starter Template")

    @mcp.tool
    def greet(name: str) -> str:
        return f"hello, {name}"

    @mcp.tool
    def get_text_chunks(text: str, chunk_size: int = 50) -> list[ContentChunk]:
        """Split text into chunks for streaming demonstration."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if chunk_size > 10000:
            raise ValueError("chunk_size too large (max 10000)")
        if len(text) > 1000000:
            raise ValueError("text too long (max 1MB)")

        return [
            ContentChunk(
                chunk_id=i // chunk_size, content=text[i : i + chunk_size], is_final=(i + chunk_size >= len(text))
            )
            for i in range(0, len(text), chunk_size)
        ]

    @mcp.tool
    def generate_sample_image() -> ImageContent:
        """Generate a minimal red-pixel PNG for binary content demo."""
        return ImageContent(
            type="image",
            data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            mimeType="image/png",
        )

    return mcp
