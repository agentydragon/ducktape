from __future__ import annotations

import inspect

from fastmcp.client import Client
from fastmcp.server.context import Context
from pydantic import BaseModel, Field
import pytest

from adgn.mcp._shared.fastmcp_flat import mcp_flat_model
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP


class InModel(BaseModel):
    a: int = Field(description="required int")
    b: str | None = Field(default=None, description="optional text")


class OutModel(BaseModel):
    ok: bool
    note: str | None = None


async def test_flat_model_infers_types_and_emits_schema():
    m = NotifyingFastMCP("decorator_test")

    @mcp_flat_model(m, structured_output=True)
    def demo(input: InModel) -> OutModel:
        """Demo tool docstring used as description."""
        return OutModel(ok=True, note=str(input.b) if input.b is not None else None)

    async with Client(m) as client:
        tools = await client.list_tools()

    assert len(tools) == 1
    t = tools[0]
    # Name defaults to function name when not provided
    assert t.name == "demo"
    assert t.description == "Demo tool docstring used as description."
    # Input schema is flat with properties a and b
    schema = t.inputSchema
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    props = schema.get("properties") or {}
    assert "a" in props
    assert "b" in props
    # Required contains 'a' (since 'b' is optional)
    required = set(schema.get("required") or [])
    assert "a" in required
    # Ensure output schema present (structured_output=True)
    out_schema = t.outputSchema
    assert isinstance(out_schema, dict)
    out_props = out_schema.get("properties") or {}
    assert "ok" in out_props


def test_flat_model_signature_exposed():
    m = NotifyingFastMCP("decorator_signature")

    @mcp_flat_model(m)
    def demo(input: InModel) -> OutModel:
        return OutModel(ok=True)

    sig = inspect.signature(demo)
    params = list(sig.parameters.values())

    # Compare whole parameter specs directly
    assert len(params) == 2
    assert (params[0].name, params[0].kind) == ("a", inspect.Parameter.KEYWORD_ONLY)
    assert (params[1].name, params[1].kind) == ("b", inspect.Parameter.KEYWORD_ONLY)


async def test_flat_model_invocation_accepts_flat_kwargs():
    m = NotifyingFastMCP("decorator_call")

    @m.flat_model()
    async def echo(input: InModel) -> OutModel:
        return OutModel(ok=input.a > 0, note=input.b)

    async with Client(m) as client:
        res = await client.call_tool("echo", {"a": 3, "b": "hi"})

    assert res.structured_content == {"ok": True, "note": "hi"}


async def test_flat_model_passes_context_kwarg():
    m = NotifyingFastMCP("decorator_context")
    seen: dict[str, Context] = {}

    @m.flat_model()
    async def capture(input: InModel, context: Context) -> OutModel:
        seen["context"] = context
        return OutModel(ok=True, note=input.b)

    async with Client(m) as client:
        await client.call_tool("capture", {"a": 5})

    assert "context" in seen
    assert isinstance(seen["context"], Context)


def test_structured_requires_return_annotation():
    m = NotifyingFastMCP("decorator_test2")

    with pytest.raises(TypeError):

        @mcp_flat_model(m, structured_output=True)
        def bad(input: InModel):  # type: ignore[no-redef]
            return {"ok": True}
