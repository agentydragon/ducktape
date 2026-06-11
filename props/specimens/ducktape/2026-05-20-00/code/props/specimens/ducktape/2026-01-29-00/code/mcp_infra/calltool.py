from __future__ import annotations

from mcp import types as mcp_types
from pydantic import TypeAdapter


def extract_structured_content[T](result: mcp_types.CallToolResult, output_type: type[T]) -> T:
    """Extract and validate structured content from a tool result.

    Raises ValueError if result is an error or lacks structured content.
    """
    if result.isError:
        raise ValueError(f"Cannot extract from error result: {result}")

    sc = result.structuredContent
    if sc is None:
        raise ValueError(f"CallToolResult missing structured content: {result}")

    return TypeAdapter(output_type).validate_python(sc)
