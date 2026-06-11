from __future__ import annotations

import pytest
from hamcrest import assert_that, has_item, instance_of

from agent_core.agent import Agent
from agent_core.events import ToolCall, ToolCallOutput
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.loop_control import RequireAnyTool
from agent_core_testing.responses import DecoratorMock
from mcp_infra.compositor.resources_server import ResourcesReadArgs
from mcp_infra.display.event_renderer import DisplayEventsHandler
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import FunctionCallItem, FunctionCallOutputItem, UserMessage


@pytest.mark.requires_docker
async def test_model_reads_container_info_with_stubbed_openai(
    reasoning_model, docker_exec_server_py312slim, compositor, compositor_client, recording_handler
) -> None:
    """Test model reading container info resources without policy gateway."""
    # Mount runtime server and capture Mounted object
    mounted_runtime = await compositor.mount_inproc(MCPMountPrefix("runtime"), docker_exec_server_py312slim)

    # Get container info URI from server instance (convert to string)
    container_info_uri = str(docker_exec_server_py312slim.container_info_resource.uri)

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        # First turn: receive request, return tool call
        _ = yield
        tool_call = m.mcp_tool_call(
            MCPMountPrefix("resources"),
            "read",
            ResourcesReadArgs(server=mounted_runtime.prefix, uri=container_info_uri, start_offset=0, max_bytes=1024),
        )
        # Second turn: receive request with tool output, return final text
        req = yield tool_call
        # Verify stateless replay: second call must include function_call and function_call_output
        assert isinstance(req.input, list)
        assert_that(req.input, has_item(instance_of(FunctionCallItem)))
        assert_that(req.input, has_item(instance_of(FunctionCallOutputItem)))
        yield m.assistant_text("ok")

    agent = await Agent.create(
        mcp_client=compositor_client,
        client=mock,
        handlers=[FinishOnTextMessageHandler(), DisplayEventsHandler(), recording_handler],
        tool_policy=RequireAnyTool(),
    )
    agent.process_message(UserMessage.text("read container info"))

    await agent.run()
    types = [e.type for e in recording_handler.records if isinstance(e, ToolCall | ToolCallOutput)]
    assert "tool_call" in types
    assert "function_call_output" in types
