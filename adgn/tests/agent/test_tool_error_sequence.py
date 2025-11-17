from __future__ import annotations

from typing import Any

from fastmcp.server import FastMCP
import pytest

from adgn.agent.agent import MiniCodex
from adgn.agent.loggers import RecordingHandler
from adgn.agent.loop_control import Auto, Continue
from adgn.agent.reducer import BaseHandler
from adgn.mcp._shared.naming import build_mcp_function
from tests.agent.ws_helpers import assert_function_call_output_structured
from tests.llm.support.openai_mock import FakeOpenAIModel


def _make_failing_server() -> FastMCP:
    mcp = FastMCP("editor")

    @mcp.tool()
    def fail(x: int) -> dict[str, Any]:
        return {"ok": False, "error": "boom"}

    return mcp


async def test_tool_error_is_surfaced_in_sequence(
    monkeypatch: pytest.MonkeyPatch, responses_factory, make_pg_compositor, approval_policy_reader_allow_all
) -> None:
    # Build in-proc failing server spec using FastMCP
    server = _make_failing_server()
    async with make_pg_compositor({"editor": server, "approval_policy": approval_policy_reader_allow_all}) as (
        mcp_client,
        _comp,
    ):
        # Create agent and run one turn

        class _AutoHandler(BaseHandler):
            def on_before_sample(self):  # type: ignore[override]
                return Continue(Auto())

        rec = RecordingHandler()

        client = FakeOpenAIModel(
            [
                responses_factory.make_tool_call(build_mcp_function("editor", "fail"), {"x": 1}),
                responses_factory.make_assistant_message("done"),
            ]
        )
        agent = await MiniCodex.create(
            model=responses_factory.model,
            mcp_client=mcp_client,
            system="You are a code agent.",
            client=client,
            handlers=[_AutoHandler(), rec],
        )
        await agent.run("call failing tool once")

    # Extract the function_call_output from the recording handler and assert failure payload surfaced
    assert_function_call_output_structured(rec.records, ok=False, error="boom")
