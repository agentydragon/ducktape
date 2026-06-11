"""Shared image reading utilities for MCP exec servers."""

from __future__ import annotations

import base64
import mimetypes
import os

import mcp.types as mcp_types

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB


class ReadImageInput(OpenAIStrictModeBaseModel):
    """Input model for read_image tool (OpenAI strict mode compatible)."""

    path: str


def validate_and_encode_image(data: bytes, path: str | os.PathLike[str]) -> mcp_types.ImageContent:
    """Validate image bytes and encode to ImageContent.

    Args:
        data: Raw image bytes
        path: Original path (used for mime type detection)

    Raises:
        ValueError: If mime type is not an image or size exceeds limit
    """
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"Not a supported image type: {path}")

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large: {len(data)} bytes (limit: {MAX_IMAGE_BYTES})")

    encoded = base64.standard_b64encode(data).decode("ascii")
    return mcp_types.ImageContent(type="image", mimeType=mime_type, data=encoded)
