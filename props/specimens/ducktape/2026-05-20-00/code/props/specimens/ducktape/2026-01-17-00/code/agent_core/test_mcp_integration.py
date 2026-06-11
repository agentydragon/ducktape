"""Tests for MCP tool integration with the agent."""

from __future__ import annotations

from hamcrest import all_of, assert_that, has_entries, has_length, has_properties, has_property, instance_of
from pydantic import BaseModel

from agent_core.agent import Agent
from agent_core.loop_control import RequireAnyTool
from agent_core_testing.matchers import assert_function_call_output_structured, has_json_arguments
from agent_core_testing.responses import EchoMock, ResponsesFactory
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.simple_servers import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME
from openai_utils.model import FunctionCallItem, SystemMessage, UserMessage

# --- Agent MCP echo tests ---


async def test_agent_mcp_echo_basic(mcp_client_echo, test_handlers, recording_handler) -> None:
    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("hello")

    agent = await Agent.create(
        mcp_client=mcp_client_echo,
        client=mock,
        handlers=test_handlers,
        tool_policy=RequireAnyTool(),
        parallel_tool_calls=False,
    )
    agent.process_message(SystemMessage.text("test: use echo"))

    await agent.run()

    assert_function_call_output_structured(recording_handler.records, has_entries(echo="hello"))


async def test_agent_mcp_echo_with_response(mcp_client_echo, test_handlers, recording_handler) -> None:
    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("hello")
        yield m.assistant_text("done")

    agent = await Agent.create(
        mcp_client=mcp_client_echo, client=mock, handlers=test_handlers, tool_policy=RequireAnyTool()
    )
    agent.process_message(UserMessage.text("say hello"))

    res = await agent.run()

    outputs = [r for r in recording_handler.records if r.type == "function_call_output"]
    assert outputs, "No tool outputs captured"
    assert outputs[0].result.structuredContent == {"echo": "hello"}
    assert res.text.strip() == "done"


# --- ResponsesFactory MCP tool call tests ---


class SampleInput(BaseModel):
    text: str
    count: int = 1


class SampleOutput(BaseModel):
    result: str


def test_responses_factory_mcp_tool_call_explicit_id(responses_factory: ResponsesFactory):
    """Test ResponsesFactory.mcp_tool_call with explicit call_id."""
    call = responses_factory.mcp_tool_call(
        ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME, SampleInput(text="hello", count=2), call_id="call_1"
    )

    assert_that(
        call,
        all_of(
            instance_of(FunctionCallItem),
            has_properties(name="echo_echo", call_id="call_1"),
            has_json_arguments({"text": "hello", "count": 2}),
        ),
    )


def test_responses_factory_mcp_tool_call_auto_id(responses_factory: ResponsesFactory):
    """Test ResponsesFactory.mcp_tool_call with auto-generated call_id."""
    call = responses_factory.mcp_tool_call(MCPMountPrefix("server"), "tool", SampleInput(text="test"))

    assert call.name == "server_tool"
    assert call.call_id.startswith("test:")  # Uses factory's call_id_prefix


def test_responses_factory_make_mcp_tool_call(responses_factory: ResponsesFactory):
    result = responses_factory.make_mcp_tool_call(
        ContainerExecServer.DOCKER_MOUNT_PREFIX, ContainerExecServer.EXEC_TOOL_NAME, SampleInput(text="ls")
    )

    assert_that(result, has_properties(id="resp_generic"))
    assert_that(result.output, has_length(1))
    call_item = result.output[0]
    assert_that(
        call_item,
        all_of(
            instance_of(FunctionCallItem),
            has_properties(name="docker_exec"),
            has_property("call_id"),  # auto-generated, just check it exists
            has_json_arguments({"text": "ls", "count": 1}),
        ),
    )


def test_responses_factory_mcp_tool_call_item(responses_factory: ResponsesFactory):
    call = responses_factory.mcp_tool_call(
        ContainerExecServer.RUNTIME_MOUNT_PREFIX, ContainerExecServer.EXEC_TOOL_NAME, SampleInput(text="echo")
    )

    assert_that(
        call,
        all_of(
            instance_of(FunctionCallItem),
            has_properties(name="runtime_exec"),
            has_json_arguments({"text": "echo", "count": 1}),
        ),
    )


def test_mcp_tool_call_composes_with_make(responses_factory: ResponsesFactory):
    result = responses_factory.make(
        responses_factory.make_item_reasoning(),
        responses_factory.mcp_tool_call(MCPMountPrefix("server"), "tool", SampleInput(text="test")),
        responses_factory.assistant_text("done"),
    )

    assert_that(result.output, has_length(3))
    _reasoning, call, _text = result.output
    assert_that(call, has_properties(name="server_tool"))
