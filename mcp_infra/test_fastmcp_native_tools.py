from __future__ import annotations

from typing import Annotated

import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.server.context import Context
from pydantic import BaseModel, Field

from mcp_infra.testing.stubs.typed_stubs import TypedClient
from mcp_infra.testing.tool_schemas import extract_tool_input_schemas, extract_tool_schemas


class EchoOutput(BaseModel):
    text: str


class SumOutput(BaseModel):
    value: int


async def test_native_tool_schema_and_typed_invocation() -> None:
    server = FastMCP("native-tools")

    @server.tool(title="Echo", output_schema=None)
    def echo(msg: Annotated[str, Field(description="Message to echo")], upper: bool = False) -> EchoOutput:
        return EchoOutput(text=msg.upper() if upper else msg)

    async with Client(server) as session:
        (tool,) = await session.list_tools()
        assert tool.name == "echo"
        assert tool.title == "Echo"
        assert tool.output_schema is None
        assert set(tool.input_schema["properties"]) == {"msg", "upper"}
        assert tool.input_schema["required"] == ["msg"]
        assert tool.input_schema["additionalProperties"] is False
        assert tool.input_schema["properties"]["msg"]["description"] == "Message to echo"

        typed = TypedClient.from_server(server, session)
        input_model = typed.models["echo"].Input
        assert input_model is not None
        result = await typed.echo(input_model(msg="hello", upper=True))

    assert isinstance(result, EchoOutput)
    assert result.text == "HELLO"


async def test_native_tool_injects_context_without_exposing_it() -> None:
    server = FastMCP("native-context")
    seen: dict[str, Context] = {}

    @server.tool(output_schema=None)
    async def capture(value: int, context: Context) -> str:
        seen["context"] = context
        return str(value)

    async with Client(server) as session:
        (tool,) = await session.list_tools()
        assert set(tool.input_schema["properties"]) == {"value"}
        await session.call_tool("capture", {"value": 7})

    assert isinstance(seen["context"], Context)


def test_native_function_signature_models_for_display() -> None:
    server = FastMCP("schema-tools")

    @server.tool(output_schema=None)
    def add(left: int, right: int = 0) -> SumOutput:
        return SumOutput(value=left + right)

    prefix = "math"
    input_schemas = extract_tool_input_schemas({prefix: server})
    output_schemas = extract_tool_schemas({prefix: server})

    input_model = input_schemas[(prefix, "add")]
    assert input_model.model_validate({"left": 3}).model_dump() == {"left": 3, "right": 0}
    assert output_schemas[(prefix, "add")] is SumOutput


if __name__ == "__main__":
    pytest_bazel.main()
