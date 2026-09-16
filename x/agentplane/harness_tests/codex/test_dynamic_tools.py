"""Codex client-supplied tools (`thread/start.dynamicTools`): a declared tool reaches the model
under its own name, and a call round-trips through `item/tool/call` server requests answered
entirely by the driver, no MCP server or built-in tool involved."""

from __future__ import annotations

from typing import Any

import pytest_bazel

from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import MODEL, CodexHarness
from x.agentplane.harness_tests.codex.responses import OpenAIResponses
from x.agentplane.native.codex import wire
from x.agentplane.native.codex.dynamic_tools import DynamicTool, DynamicToolServer

TOOL_NAME = "lookup"


def _lookup(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "inputText", "text": f"LOOKUP: {arguments['note']}"}]


TOOLS = DynamicToolServer(
    {
        TOOL_NAME: DynamicTool(
            name=TOOL_NAME,
            description="Looks up a note and echoes it back.",
            input_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
            handler=_lookup,
        )
    }
)


async def test_dynamic_tool_is_declared_and_called(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses, dynamic_tools=TOOLS) as run:
        turn = await run.start_turn("Use the lookup tool on 'hi' and report the result.")

        async with await openai_responses.await_next_request() as exchange:
            # The declared tool already rode the model's very first request; no separate discovery
            # round trip is needed for a tool the driver declared up front.
            assert TOOL_NAME in exchange.request.tool_names
            stream = sse.response_stream([sse.FunctionCall("call_test_1", TOOL_NAME, {"note": "hi"})], model=MODEL)
            await exchange.send(*stream.events)

        async with await openai_responses.await_next_request() as exchange:
            (result,) = exchange.request.function_call_outputs
            assert result.call_id == "call_test_1"
            assert result.output == "LOOKUP: hi"
            stream = sse.response_stream([sse.Message("LOOKUP_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    frames.assert_success(captured, "LOOKUP_DONE")

    # `frames.items()` returns both the started and completed record for one logical item; the
    # extra fields of `dynamicToolCall` (an `UnknownItem`) are not individually modeled.
    dynamic_calls = [item for item in frames.items(captured) if item.type == "dynamicToolCall"]
    assert {item.id for item in dynamic_calls} == {"call_test_1"}
    completed_extra = next(
        (
            item.model_extra
            for item in dynamic_calls
            if item.model_extra and item.model_extra.get("status") == "completed"
        ),
        None,
    )
    assert completed_extra is not None
    assert completed_extra["success"] is True
    assert completed_extra["contentItems"] == [{"type": "inputText", "text": "LOOKUP: hi"}]


if __name__ == "__main__":
    pytest_bazel.main()
