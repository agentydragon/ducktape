from __future__ import annotations

from fastmcp.client import Client
from fastmcp.exceptions import ToolError


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
        raise RuntimeError(f"{name} failed (is_error=True)")
