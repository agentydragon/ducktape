"""Test that tool call errors don't abort the agent turn.

This test verifies that when a tool call returns an error (e.g., validation error),
the agent continues with the next phase instead of aborting the entire turn.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp.server import FastMCP
from hamcrest import assert_that, contains_string
import pytest

from adgn.agent.agent import MiniCodex
from adgn.agent.loggers import RecordingHandler
from adgn.agent.loop_control import Auto, Continue
from adgn.agent.reducer import BaseHandler
from adgn.mcp._shared.naming import build_mcp_function
from tests.agent.ws_helpers import assert_function_call_output_structured
from tests.llm.support.openai_mock import FakeOpenAIModel


def _make_validation_server() -> FastMCP:
    """Create a server with a tool that validates input strictly."""
    mcp = FastMCP("validator")

    @mcp.tool()
    def send_message(mime: Literal["text/markdown"], content: str) -> dict[str, Any]:
        # FastMCP will automatically validate the mime parameter
        return {"ok": True, "message": content}

    return mcp


async def test_tool_error_continues_turn(
    monkeypatch: pytest.MonkeyPatch, responses_factory, make_pg_compositor, approval_policy_reader_allow_all
) -> None:
    """Test that a tool validation error doesn't abort the turn.

    The agent should:
    1. Call the tool with wrong mime type (text/plain)
    2. Get a validation error
    3. Continue to the next phase
    4. Retry with correct mime type (text/markdown)
    5. Successfully complete
    """
    server = _make_validation_server()
    async with make_pg_compositor({"validator": server, "approval_policy": approval_policy_reader_allow_all}) as (
        mcp_client,
        _comp,
    ):

        class _AutoHandler(BaseHandler):
            def on_before_sample(self):  # type: ignore[override]
                return Continue(Auto())

        rec = RecordingHandler()

        # Simulate the agent trying with wrong mime, then correcting itself
        client = FakeOpenAIModel(
            [
                # First attempt with wrong mime type
                responses_factory.make_tool_call(
                    build_mcp_function("validator", "send_message"), {"mime": "text/plain", "content": "Hello"}
                ),
                # After error, agent retries with correct mime type
                responses_factory.make_tool_call(
                    build_mcp_function("validator", "send_message"), {"mime": "text/markdown", "content": "Hello"}
                ),
                # Final message
                responses_factory.make_assistant_message("Successfully sent message"),
            ]
        )

        agent = await MiniCodex.create(
            model=responses_factory.model,
            mcp_client=mcp_client,
            system="You are a helpful assistant. Use the validator tools.",
            client=client,
            handlers=[_AutoHandler(), rec],
        )

        result = await agent.run("Send a greeting")

    # Verify the sequence of events
    tool_calls = [evt for evt in rec.records if evt.get("kind") == "tool_call"]
    outputs = [evt for evt in rec.records if evt.get("kind") == "function_call_output"]

    # Should have 2 tool calls
    assert len(tool_calls) == 2, f"Expected 2 tool calls, got {len(tool_calls)}"

    # First call should fail with validation error
    first_output = outputs[0]
    assert first_output["result"].get("is_error") is True
    error_content = first_output["result"]["content"][0]["text"]
    # Hamcrest contains-string checks for clarity
    assert_that(error_content.lower(), contains_string("error"))
    assert "text/markdown" in error_content or "literal" in error_content.lower()

    # Second call should succeed
    second_output = outputs[1]
    assert second_output["result"].get("is_error") is False
    assert_function_call_output_structured([second_output], ok=True)

    # Final result should contain the success message
    assert_that(result.text, contains_string("Successfully sent message"))
