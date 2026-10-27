from __future__ import annotations

from typing import Annotated

import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.server.context import Context
from pydantic import BaseModel, Field


class EchoOutput(BaseModel):
    text: str


async def test_native_tool_schema_and_invocation() -> None:
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

        response = await session.call_tool("echo", {"msg": "hello", "upper": True})
        result = EchoOutput.model_validate(response.structured_content)

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


if __name__ == "__main__":
    pytest_bazel.main()
