from __future__ import annotations

from typing import Any, TypeVar

from fastmcp.client.client import ClientSession
from mcp import types as mcp_types
from pydantic import TypeAdapter

from .urls import parse_any_url


def extract_single_text_content(res: mcp_types.ReadResourceResult) -> str:
    """Return the single text part from a ReadResourceResult or raise.

    - Requires exactly one TextResourceContents part.
    - Raises RuntimeError if zero or multiple text parts are present, or if any
      non-text part is present.
    """
    text_parts = [p for p in res.contents if isinstance(p, mcp_types.TextResourceContents)]
    blob_parts = [p for p in res.contents if isinstance(p, mcp_types.BlobResourceContents)]
    if blob_parts:
        raise RuntimeError("expected a single text part, found blob content")
    if len(text_parts) != 1:
        raise RuntimeError(f"expected exactly one text part, found {len(text_parts)}")
    text: str | None = text_parts[0].text
    if text is None:
        raise RuntimeError("text content part missing text payload")
    return text


async def read_text_json(session: ClientSession, uri: str) -> Any:
    """Read a text JSON resource and parse it to a Python value.

    - Validates that the payload is exactly one text part.
    - Parses the text as JSON using Pydantic's TypeAdapter(dict) to preserve types.
    """
    rr = await session.read_resource(parse_any_url(uri))
    s = extract_single_text_content(rr)
    # Parse as JSON into a generic Python structure
    return TypeAdapter(dict[str, Any]).validate_json(s)


# Internal helpers; import explicitly where needed

T = TypeVar("T")


async def read_text_json_typed(session: ClientSession, uri: str, model: type[T]) -> T:
    """Read a text JSON resource and parse it as the given Pydantic model/type.

    - Validates exactly one text part
    - Parses JSON into the provided model/type using TypeAdapter(model).validate_json
    """
    rr = await session.read_resource(parse_any_url(uri))
    s = extract_single_text_content(rr)
    return TypeAdapter(model).validate_json(s)
