from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pydantic_core
from fastmcp.client import Client
from fastmcp.client.client import CallToolResult as FastMCPCallToolResult
from fastmcp.exceptions import ToolError
from mcp import types as mcp_types


def extract_text_blocks(contents: Iterable[mcp_types.ContentBlock]) -> list[str]:
    return [
        block.text
        for block in contents
        if isinstance(block, mcp_types.TextContent) and isinstance(block.text, str) and block.text
    ]


def extract_error_detail_from_fastmcp(res: FastMCPCallToolResult) -> str | None:
    """Best-effort string representation of a FastMCP tool error result.

    TODO: This pattern is bad - it guesses at error structure by probing for common field
    names. Replace with either: (a) typed error models that tools actually return, validated
    at the boundary, or (b) just use the raw structured_content/content without speculation.
    See also: handler.py BootstrapHandler, reducer.py, rich_display.py.
    """
    detail: str | None = None
    try:
        sc: Any = res.structured_content
        if isinstance(sc, dict) and sc:
            for key in ("message", "reason", "error", "detail"):
                val = sc.get(key)
                if isinstance(val, str) and val:
                    detail = val
                    break
            if detail is None:
                detail = pydantic_core.to_json(sc, fallback=str).decode("utf-8")[:200]
        if not detail:
            texts = extract_text_blocks(res.content or [])
            if texts:
                detail = " | ".join(texts)[:200]
    except (KeyError, TypeError, AttributeError, ValueError):
        # Best-effort extraction; suppress malformed responses
        detail = None
    return detail


async def call_simple_ok(client: Client, *, name: str, arguments: dict) -> None:
    """Call a simple tool and ensure it did not error.

    - Invokes the tool via Client.call_tool to propagate ToolError directly
    - Requires is_error == False
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
        detail = extract_error_detail_from_fastmcp(res)
        if detail:
            raise RuntimeError(f"{name} failed: {detail}")
        raise RuntimeError(f"{name} failed")
