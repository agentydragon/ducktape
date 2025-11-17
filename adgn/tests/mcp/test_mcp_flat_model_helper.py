from __future__ import annotations

from typing import Literal

from fastmcp.client import Client
from fastmcp.server import FastMCP
from pydantic import BaseModel, Field
import pytest

from adgn.mcp._shared.fastmcp_flat import FlatModelFastMCP, mcp_flat_model


class EchoInput(BaseModel):
    msg: str = Field(description="Message to echo")
    upper: bool = Field(default=False, description="Uppercase the message")


class EchoOutput(BaseModel):
    kind: Literal["Echo"] = "Echo"
    text: str


def make_echo_server():
    mcp = FlatModelFastMCP("echo")

    @mcp.tool(name="echo", title="Echo", description="Echo a message", structured_output=True, flat=True)
    def echo(input: EchoInput) -> EchoOutput:
        text = input.msg.upper() if input.upper else input.msg
        return EchoOutput(text=text)

    return mcp


async def test_flat_schema_and_typed_invocation(make_typed_mcp):
    server = make_echo_server()

    async with make_typed_mcp(server, "echo") as (client, sess):
        # Fast path: typed client can call tool like client.echo(EchoInput(...)) -> EchoOutput
        echo_input_class = client.models["echo"].Input
        assert echo_input_class is EchoInput

        out = await client.echo(EchoInput(msg="hi", upper=True))
        assert isinstance(out, EchoOutput)
        assert out.text == "HI"

        # Validate server advertises flat arguments (no nested 'input')
        tools = await sess.list_tools()
        tool = next(t for t in tools if t.name == "echo")
        schema = tool.inputSchema or {}
        props = schema.get("properties", {})
        assert set(props.keys()) >= {"msg", "upper"}  # flat keys present
        # Ensure not wrapped
        assert "input" not in props


@pytest.fixture
def list_tools_via_client():
    async def _list(server: FastMCP):
        async with Client(server) as client:
            return await client.list_tools()

    return _list


async def test_mcp_flat_model_backward_compatibility(list_tools_via_client):
    legacy = FastMCP("legacy")

    @mcp_flat_model(legacy, name="legacy_echo", structured_output=False)
    def legacy_echo(input: EchoInput):
        return {"text": input.msg}

    tools = await list_tools_via_client(legacy)
    tool = next(t for t in tools if t.name == "legacy_echo")
    props = (tool.inputSchema or {}).get("properties", {})
    assert "msg" in props
    assert "upper" in props
    assert "input" not in props


async def test_tool_flat_explicit_models(list_tools_via_client):
    mcp = FlatModelFastMCP("echo2")

    @mcp.tool(name="echo", flat=True, flat_output_model=EchoOutput)
    def echo_again(payload: EchoInput) -> EchoOutput:
        return EchoOutput(text=payload.msg)

    tools = await list_tools_via_client(mcp)
    tool = next(t for t in tools if t.name == "echo")
    props = (tool.inputSchema or {}).get("properties", {})
    assert set(props) >= {"msg", "upper"}
