from __future__ import annotations

from collections.abc import Iterable
import json
from typing import TypeVar

from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from mcp import types as mcp_types
from pydantic import BaseModel, TypeAdapter, ValidationError

from adgn.mcp._shared.calltool import to_pydantic

T_In = TypeVar("T_In", bound=BaseModel)
T_Out = TypeVar("T_Out")


def extract_text_blocks(contents: Iterable[mcp_types.ContentBlock]) -> list[str]:
    return [
        block.text
        for block in contents
        if isinstance(block, mcp_types.TextContent) and isinstance(block.text, str) and block.text
    ]


def extract_error_detail(res: mcp_types.CallToolResult) -> str | None:
    """Best-effort string representation of an MCP tool error result."""
    detail: str | None = None
    try:
        sc = res.structuredContent
        if isinstance(sc, dict) and sc:
            for key in ("message", "reason", "error", "detail"):
                val = sc.get(key)
                if isinstance(val, str) and val:
                    detail = val
                    break
            if detail is None:
                detail = json.dumps(sc, ensure_ascii=False)[:200]
        if not detail:
            texts = extract_text_blocks(res.content or [])
            if texts:
                detail = " | ".join(texts)[:200]
    except Exception:
        detail = None
    return detail


async def call_simple_ok(client: Client, *, name: str, arguments: dict) -> None:
    """Call a simple tool and ensure it did not error.

    - Invokes the tool via Client.call_tool to propagate ToolError directly
    - Requires a typed CallToolResult with isError == False
    - Raises RuntimeError with a readable operation name on failure
    """
    try:
        # client.call_tool preserves fastmcp.exceptions.ToolError, which is necessary
        # for tests that assert reserved policy errors bubble through untouched.
        res = await client.call_tool(name=name, arguments=arguments, raise_on_error=True)
    except ToolError:
        raise
    except Exception as exc:
        raise RuntimeError(f"{name} failed: {exc}") from exc
    if bool(res.is_error):
        detail = extract_error_detail(to_pydantic(res))
        if detail:
            raise RuntimeError(f"{name} failed: {detail}")
        raise RuntimeError(f"{name} failed")


# Type-safe tool calling with Pydantic models

StructuredContent = BaseModel | dict[str, object] | list[object] | str | int | float | bool | None


def _structured_content(result: mcp_types.CallToolResult, *, tool_name: str) -> StructuredContent:
    from typing import cast

    sc = cast(StructuredContent | None, result.structuredContent)
    if sc is None:
        raise RuntimeError(f"{tool_name!r} did not return structuredContent; requires structured outputs")
    return sc


async def _call_normalized(
    session: Client, tool_name: str, arguments: dict[str, object] | None
) -> mcp_types.CallToolResult:
    """Call a FastMCP tool and normalize the result to the Pydantic CallToolResult."""
    raw = await session.call_tool(name=tool_name, arguments=arguments)
    return to_pydantic(raw)


async def _call_structured(
    session: Client, tool_name: str, arguments: dict[str, object] | None
) -> tuple[mcp_types.CallToolResult, StructuredContent]:
    """Call a FastMCP tool and return both the normalized result and structured content."""
    result = await _call_normalized(session, tool_name, arguments)
    return result, _structured_content(result, tool_name=tool_name)


async def call_tool_typed(
    session: Client, name: str, payload: T_In, out_type: type[T_Out], *, exclude_none: bool = True
) -> T_Out:
    """Call an MCP tool with a Pydantic input and parse a Pydantic output.

    Requires structuredContent from the server; raises otherwise.

    Args:
        session: MCP client session
        name: Tool name
        payload: Pydantic model instance (validated input)
        out_type: Expected output model type
        exclude_none: Whether to exclude None values from serialization

    Returns:
        Validated output model instance

    Raises:
        ValidationError: If output doesn't match out_type
        RuntimeError: If server doesn't return structuredContent
    """
    args = payload.model_dump(exclude_none=exclude_none)
    _result, structured = await _call_structured(session, name, args)
    adapter: TypeAdapter[T_Out] = TypeAdapter(out_type)
    try:
        parsed = adapter.validate_python(structured)
    except ValidationError:
        if isinstance(structured, dict) and "result" in structured:
            parsed = adapter.validate_python(structured["result"])
        else:
            raise
    return parsed
