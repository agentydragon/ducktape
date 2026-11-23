"""Shared image reading utilities for MCP exec servers."""

from __future__ import annotations

import base64
import mimetypes
import os

from mcp.types import ImageContent

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB

# Intersection of formats accepted by both OpenAI and Anthropic vision APIs.
SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def validate_and_encode_image(data: bytes, path: str | os.PathLike[str]) -> ImageContent:
    """Validate image bytes and encode to ImageContent.

    `path` only drives mime type detection. Raises ValueError if the mime type is not an image or the size exceeds
    the limit.
    """
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type or mime_type not in SUPPORTED_MIME_TYPES:
        raise ValueError(
            f"Unsupported image type for {path}: {mime_type}. Supported: {', '.join(sorted(SUPPORTED_MIME_TYPES))}"
        )

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large: {len(data)} bytes (limit: {MAX_IMAGE_BYTES})")

    encoded = base64.standard_b64encode(data).decode("ascii")
    return ImageContent(type="image", mimeType=mime_type, data=encoded)
