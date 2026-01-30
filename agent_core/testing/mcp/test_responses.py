"""Tests for MCPResponsesFactory convenience methods."""

from __future__ import annotations

import pytest_bazel
from hamcrest import all_of, assert_that, has_length, has_properties, instance_of
from pydantic import BaseModel

from agent_core.testing.matchers import has_json_arguments
from agent_core.testing.mcp.responses import MCPResponsesFactory
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.simple_servers import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME
from openai_utils.model import FunctionCallItem


class SampleInput(BaseModel):
    text: str
    count: int = 1


def test_responses_factory_mcp_tool_call_explicit_id(responses_factory: MCPResponsesFactory):
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


def test_responses_factory_mcp_tool_call_auto_id(responses_factory: MCPResponsesFactory):
    """Test ResponsesFactory.mcp_tool_call with auto-generated call_id."""
    call = responses_factory.mcp_tool_call(MCPMountPrefix("server"), "tool", SampleInput(text="test"))

    assert call.name == "server_tool"
    assert call.call_id.startswith("test:")  # Uses factory's call_id_prefix


def test_responses_factory_mcp_tool_call_item(responses_factory: MCPResponsesFactory):
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


def test_mcp_tool_call_composes_with_make(responses_factory: MCPResponsesFactory):
    result = responses_factory.make(
        responses_factory.make_item_reasoning(),
        responses_factory.mcp_tool_call(MCPMountPrefix("server"), "tool", SampleInput(text="test")),
        responses_factory.assistant_text("done"),
    )

    assert_that(result.output, has_length(3))
    _reasoning, call, _text = result.output
    assert_that(call, has_properties(name="server_tool"))


if __name__ == "__main__":
    pytest_bazel.main()
