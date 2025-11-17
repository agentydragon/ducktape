from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastmcp.client.client import CallToolResult
from mcp import types
import pytest

from adgn.agent.agent import MiniCodex
from adgn.agent.reducer import AutoHandler, BaseHandler
from adgn.mcp._shared.calltool import to_pydantic
from adgn.mcp._shared.naming import build_mcp_function
from tests.llm.support.openai_mock import FakeOpenAIModel


@dataclass
class Record:
    assistant_text: list[str]
    tool_outputs: list[dict[str, Any]]


class RecordingHandler(BaseHandler):
    def __init__(self, rec: Record) -> None:
        self.rec = rec

    def on_assistant_text_event(self, evt: Any) -> None:  # evt has .text
        self.rec.assistant_text.append(getattr(evt, "text", ""))

    def on_tool_result_event(self, evt) -> None:
        res = evt.result
        if isinstance(res, CallToolResult):
            res = to_pydantic(res)
        if not isinstance(res, types.CallToolResult):
            raise TypeError(f"unexpected tool result type: {type(res).__name__}")
        self.rec.tool_outputs.append(res)


async def test_agent_mcp_echo_tool_use(
    monkeypatch: pytest.MonkeyPatch, responses_factory, make_pg_compositor_echo
) -> None:
    # Provide a two-step sequence via our shared Pydantic fake client
    client = FakeOpenAIModel(
        [
            responses_factory.make_tool_call(build_mcp_function("echo", "echo"), {"text": "hello"}),
            responses_factory.make_assistant_message("done"),
        ]
    )

    rec = Record(assistant_text=[], tool_outputs=[])

    async with make_pg_compositor_echo() as (mcp_client, _comp):
        agent = await MiniCodex.create(
            model="test-model",
            mcp_client=mcp_client,
            system="You are a test agent.",
            client=client,
            handlers=[AutoHandler(), RecordingHandler(rec)],
            parallel_tool_calls=False,
        )
        async with agent:
            res = await agent.run(user_text="use echo")

    # The tool output should be emitted (ToolCallOutput) and assistant text should follow
    assert rec.tool_outputs, "No tool outputs captured"
    first = rec.tool_outputs[0]
    assert isinstance(first, types.CallToolResult)
    assert first.structuredContent == {"echo": "hello"}
    assert res.text.strip() == "done"
