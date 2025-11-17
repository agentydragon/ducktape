from __future__ import annotations

import pytest

from adgn.agent.agent import MiniCodex
from adgn.agent.event_renderer import DisplayEventsHandler
from adgn.agent.loggers import RecordingHandler
from adgn.agent.reducer import AutoHandler
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.resources.server import ResourcesReadArgs
from adgn.openai_utils.model import FunctionCallItem, FunctionCallOutputItem
from tests.llm.support.openai_mock import FakeOpenAIModel


@pytest.mark.requires_docker
async def test_model_reads_container_info_with_stubbed_openai(
    reasoning_model, responses_factory, docker_inproc_spec_alpine, make_pg_compositor, approval_policy_reader_allow_all
) -> None:
    async with make_pg_compositor(
        {"runtime": docker_inproc_spec_alpine, "approval_policy": approval_policy_reader_allow_all}
    ) as (mcp_client, _comp):
        # Prepare a deterministic two-step sequence: function_call then final text
        ResourcesReadArgs(server="docker", uri="resource://container.info", max_bytes=1024)
        seq = [
            responses_factory.make_tool_call(
                build_mcp_function("resources", "read"),
                {"server": "docker", "uri": "resource://container.info", "start_offset": 0, "max_bytes": 1024},
            ),
            responses_factory.make_assistant_message("ok"),
        ]
        client = FakeOpenAIModel(seq)
        rec = RecordingHandler()  # from adgn.agent.loggers
        agent = await MiniCodex.create(
            model=reasoning_model,
            mcp_client=mcp_client,
            client=client,
            system="test",
            handlers=[AutoHandler(), DisplayEventsHandler(), rec],
        )

        await agent.run("read container info")
        kinds = [e.get("kind") for e in rec.records]
        assert "tool_call" in kinds
        assert "function_call_output" in kinds
        assert client.calls == 2
        # Verify that the second call included the function_call and function_call_output (stateless replay).
        second = client.captured[1]
        input_items = list(second.input or [])
        assert any(isinstance(it, FunctionCallItem) for it in input_items), (
            f"Expected FunctionCallItem in next-turn input: {input_items}"
        )
        assert any(isinstance(it, FunctionCallOutputItem) for it in input_items), (
            f"Expected FunctionCallOutputItem in next-turn input: {input_items}"
        )
