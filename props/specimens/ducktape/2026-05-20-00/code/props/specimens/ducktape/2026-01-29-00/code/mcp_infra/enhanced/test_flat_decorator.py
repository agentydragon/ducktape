from __future__ import annotations

import inspect
import json
from typing import Literal

import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.server.context import Context
from pydantic import Field, field_validator

from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.stubs.typed_stubs import TypedClient
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel


class InModel(OpenAIStrictModeBaseModel):
    a: int = Field(description="required int")
    b: str | None = Field(default=None, description="optional text")


class OutModel(OpenAIStrictModeBaseModel):
    ok: bool
    note: str | None = None


async def test_flat_model_infers_types_and_emits_schema():
    m = EnhancedFastMCP("decorator_test")

    @m.flat_model()
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
    # Output schema not present by default (structured_output=False by default)
    out_schema = t.outputSchema
    assert out_schema is None


def test_flat_model_signature_exposed():
    m = EnhancedFastMCP("decorator_signature")

    @m.flat_model()
    def demo(input: InModel) -> OutModel:
        return OutModel(ok=True)

    # flat_model() returns FlatTool; .fn is the original function (not a wrapper)
    # The flattening happens at the JSON Schema level for MCP, not in Python signature
    sig = inspect.signature(demo.fn)
    params = list(sig.parameters.values())

    # Original function has 1 parameter: the input model
    assert len(params) == 1
    assert params[0].name == "input"


async def test_flat_model_invocation_accepts_flat_kwargs():
    m = EnhancedFastMCP("decorator_call")

    @m.flat_model()
    async def echo(input: InModel) -> OutModel:
        return OutModel(ok=input.a > 0, note=input.b)

    async with Client(m) as client:
        res = await client.call_tool("echo", {"a": 3, "b": "hi"})

    assert res.structured_content == {"ok": True, "note": "hi"}


async def test_flat_model_passes_context_kwarg():
    m = EnhancedFastMCP("decorator_context")
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
    m = EnhancedFastMCP("decorator_test2")

    # With structured_output=True, return annotation is required
    with pytest.raises(TypeError):

        @m.flat_model(structured_output=True)
        def bad(input: InModel):
            return {"ok": True}


# Models for union return test


class PageA(OpenAIStrictModeBaseModel):
    type: Literal["A"] = "A"
    value: str


class PageB(OpenAIStrictModeBaseModel):
    type: Literal["B"] = "B"
    number: int


UnionResult = PageA | PageB


class EmptyInput(OpenAIStrictModeBaseModel):
    pass


async def test_flat_model_union_return_unwrapped():
    """Test that union return types are properly unwrapped via .data field.

    FastMCP wraps non-object schemas (unions, primitives) in {'result': ...} for MCP protocol,
    but provides .data field with unwrapped content (see fastmcp/src/fastmcp/client/client.py).
    """
    m = EnhancedFastMCP("union_test")

    @m.flat_model(structured_output=True)
    def get_page(input: EmptyInput) -> UnionResult:
        return PageA(value="hello")

    async with Client(m) as client:
        # Verify FastMCP sets x-fastmcp-wrap-result for union types
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "get_page")
        assert tool.outputSchema["x-fastmcp-wrap-result"] is True
        assert "result" in tool.outputSchema["properties"]

        # Verify raw result has wrapping
        result = await client.call_tool("get_page", {})
        assert result.structured_content == {"result": {"type": "A", "value": "hello"}}
        assert result.data == {"type": "A", "value": "hello"}

        # Verify TypedClient unwraps via .data field
        typed_client = TypedClient.from_server(m, client)
        page = await typed_client.get_page(EmptyInput())
        assert isinstance(page, PageA)
        assert page.value == "hello"


class StrictModel(OpenAIStrictModeBaseModel):
    """Model for validation error testing with custom validator."""

    value: int

    @field_validator("value")
    @classmethod
    def check_positive(cls, v):
        if v <= 0:
            raise ValueError("must be positive")
        return v


async def test_flat_model_validation_error_formatting():
    """Test that Pydantic validation errors are formatted as structured JSON without URL.

    This tests the case where FastMCP's parameter validation passes (all required args provided)
    but Pydantic model construction fails (custom validator rejection).
    """
    m = EnhancedFastMCP("validation_test")

    @m.flat_model()
    async def validate_input(input: StrictModel) -> OutModel:
        return OutModel(ok=True)

    async with Client(m) as client:
        # Call with value that fails custom validator
        result = await client.call_tool("validate_input", {"value": -5}, raise_on_error=False)
        assert result.is_error

        # Parse the error message as JSON
        error_json = json.loads(result.content[0].text)
        assert isinstance(error_json, list)
        assert len(error_json) == 1

        err = error_json[0]
        # Verify structure
        assert err["type"] == "value_error"
        assert err["loc"] == ["value"]
        assert "must be positive" in err["msg"]
        assert err["input"] == -5
        assert err["ctx"] == {"error": "must be positive"}
        # Verify no URL
        assert "url" not in err


if __name__ == "__main__":
    pytest_bazel.main()
