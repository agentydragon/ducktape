from __future__ import annotations

from typing import Literal

import pytest
from fastmcp.client import Client
from fastmcp.server import FastMCP
from pydantic import Field

from mcp_infra.enhanced.server import EnhancedFastMCP
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel


class EchoInput(OpenAIStrictModeBaseModel):
    msg: str = Field(description="Message to echo")
    upper: bool = Field(default=False, description="Uppercase the message")


class EchoOutput(OpenAIStrictModeBaseModel):
    kind: Literal["Echo"] = "Echo"
    text: str


@pytest.fixture
def echo_server():
    """Echo server for testing flat model helpers."""

    mcp = EnhancedFastMCP("echo")

    @mcp.flat_model(title="Echo")
    def echo(input: EchoInput) -> EchoOutput:
        text = input.msg.upper() if input.upper else input.msg
        return EchoOutput(text=text)

    return mcp


async def test_flat_schema_and_typed_invocation(make_typed_mcp, echo_server):
    async with make_typed_mcp(echo_server) as (client, sess):
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
    # Structured output is now always enabled, but flat model still works

    legacy = EnhancedFastMCP("legacy")

    @legacy.flat_model(name="legacy_echo")
    def legacy_echo(input: EchoInput) -> EchoOutput:
        return EchoOutput(text=input.msg)

    tools = await list_tools_via_client(legacy)
    tool = next(t for t in tools if t.name == "legacy_echo")
    props = (tool.inputSchema or {}).get("properties", {})
    assert "msg" in props
    assert "upper" in props
    assert "input" not in props


async def test_tool_flat_explicit_models(list_tools_via_client):
    # Output model is now always inferred from return annotation

    mcp = EnhancedFastMCP("echo2")

    @mcp.flat_model()
    def echo(payload: EchoInput) -> EchoOutput:
        return EchoOutput(text=payload.msg)

    tools = await list_tools_via_client(mcp)
    tool = next(t for t in tools if t.name == "echo")
    props = (tool.inputSchema or {}).get("properties", {})
    assert set(props) >= {"msg", "upper"}
