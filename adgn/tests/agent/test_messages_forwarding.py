from __future__ import annotations

from typing import Any

import pytest

from adgn.agent.agent import MiniCodex
from adgn.agent.reducer import AutoHandler
from adgn.mcp._shared.naming import build_mcp_function
from adgn.openai_utils.model import (
    AssistantMessage,
    FunctionCallItem,
    FunctionCallOutputItem,
    ReasoningItem,
    ResponsesResult,
)
from tests.agent.ui.typed_asserts import assert_items_exclude_instance, assert_items_include_instances
from tests.fixtures.responses import ResponsesFactory
from tests.llm.support.openai_mock import FakeOpenAIModel


@pytest.fixture
def approval_policy_reader_allow_all(approval_policy_reader_stub):
    """Override policy reader with a stub that approves without Docker."""
    return approval_policy_reader_stub


# Use our shared Pydantic-only fake model client

# Examples and references:
# - Demo scripts: :/adgn/examples/openai_api/stateless_two_step_demo.py (text & tools stateless 2-step continuation)
# - OpenAI Responses API reference: https://platform.openai.com/docs/api-reference/responses
# - OpenAI Cookbook examples (reasoning & function-call orchestration):
#   - reasoning_items.ipynb: https://github.com/openai/openai-cookbook/blob/main/examples/responses_api/reasoning_items.ipynb
#   - reasoning_function_calls.ipynb: https://github.com/openai/openai-cookbook/blob/main/examples/reasoning_function_calls.ipynb


def _make_reasoning_then_message(text: str, rf: ResponsesFactory) -> ResponsesResult:
    # Ensure unique item IDs per response to avoid duplicate-id assertions in agent transcript
    result: ResponsesResult = rf.make(rf.make_item_reasoning(), rf.assistant_text(text))
    return result


def _make_tool_call_resp(
    name: str, args: dict[str, Any], rf: ResponsesFactory, *, call_id: str | None = None
) -> ResponsesResult:
    result: ResponsesResult = rf.make_tool_call(name, args, call_id)
    return result


async def test_stateless_reasoning_forwarding(make_pg_compositor_echo, responses_factory: ResponsesFactory) -> None:
    """Request1 produces reasoning+assistant; Request2 should include reasoning in input."""

    seq = [_make_reasoning_then_message("ok", responses_factory)]
    client = FakeOpenAIModel(seq)

    async with make_pg_compositor_echo() as (mcp_client, _comp):
        agent = await MiniCodex.create(
            model="test-model", mcp_client=mcp_client, system="test", client=client, handlers=[AutoHandler()]
        )

        await agent.run("say hi")

        # Reasoning should be present in the agent transcript/messages for stateless forwarding
        msgs = agent.messages

        # Typed presence checks using Hamcrest (combined)
        assert_items_include_instances(msgs, ReasoningItem, AssistantMessage)


async def test_function_call_and_function_call_output_replay(
    make_pg_compositor_echo, responses_factory: ResponsesFactory
) -> None:
    """Request1 produces a function_call; after local execution, messages() must include function_call and function_call_output."""

    seq = [
        _make_tool_call_resp(build_mcp_function("echo", "echo"), {"text": "hi"}, responses_factory),
        _make_reasoning_then_message("done", responses_factory),
    ]
    client = FakeOpenAIModel(seq)

    async with make_pg_compositor_echo() as (mcp_client, _comp):
        agent = await MiniCodex.create(
            model="test-model", mcp_client=mcp_client, system="test", client=client, handlers=[AutoHandler()]
        )

        await agent.run("say hi")

    # Check that the captured second input includes function_call and function_call_output
    # We should have exactly two calls; inspect the second call's input shape
    assert client.calls == 2
    # Captured request input holds our typed InputItem models
    input_items = list(client.captured[1].input or [])
    assert_items_include_instances(input_items, FunctionCallItem, FunctionCallOutputItem)


async def test_mixed_reasoning_fc_ordering(make_pg_compositor_echo, responses_factory: ResponsesFactory) -> None:
    """Resp1 returns reasoning, function_call, assistant; after function_call_output, messages preserves order
    reasoning, function_call, function_call_output, assistant.
    """

    # Build a response with reasoning then function_call then assistant (our facade types)
    resp = responses_factory.make(
        responses_factory.make_item_reasoning(),
        responses_factory.tool_call(build_mcp_function("echo", "echo"), {"text": "hi"}),
        responses_factory.assistant_text("done"),
    )
    # Use a final assistant message on the second call to avoid infinite tool-call loops
    client = FakeOpenAIModel([resp, _make_reasoning_then_message("ok", responses_factory)])

    async with make_pg_compositor_echo() as (mcp_client, _comp):
        agent = await MiniCodex.create(
            model="test-model", mcp_client=mcp_client, system="test", client=client, handlers=[AutoHandler()]
        )
        await agent.run("start")

    # Expect exactly two calls; validate second call input ordering/types (typed InputItems)
    assert client.calls == 2
    input_items = list(client.captured[1].input or [])
    assert_items_include_instances(
        input_items, ReasoningItem, FunctionCallItem, FunctionCallOutputItem, AssistantMessage
    )


async def test_no_synthesized_reasoning_items(make_pg_compositor_echo, responses_factory: ResponsesFactory) -> None:
    """Ensure agent does not fabricate reasoning rs_* items when missing."""

    # Response with only a function_call (no reasoning)
    seq = [
        _make_tool_call_resp(build_mcp_function("echo", "echo"), {"text": "hi"}, responses_factory),
        _make_reasoning_then_message("done", responses_factory),
    ]
    client = FakeOpenAIModel(seq)

    async with make_pg_compositor_echo() as (mcp_client, _comp):
        agent = await MiniCodex.create(
            model="test-model", mcp_client=mcp_client, system="test", client=client, handlers=[AutoHandler()]
        )
        await agent.run("say hi")

    idx = min(1, len(client.captured) - 1)
    input_items = list(client.captured[idx].input or [])
    # No synthesized ReasoningItem entries should be present
    assert_items_exclude_instance(input_items, ReasoningItem)


async def test_model_provided_tool_output_records_without_execution(
    responses_factory: ResponsesFactory, make_pg_compositor_echo
) -> None:
    """If the model supplies tool output inline, agent should not run the tool again."""

    seq = [
        responses_factory.make_tool_call_with_output(build_mcp_function("echo", "echo"), {"text": "hi"}, {"echo": "hi"})
    ]
    client = FakeOpenAIModel(seq)

    async with make_pg_compositor_echo() as (mcp_client, _comp):
        agent = await MiniCodex.create(
            model="test-model", mcp_client=mcp_client, system="test", client=client, handlers=[AutoHandler()]
        )

        await agent.run("say hi")

    msgs = agent.messages
    assert_items_include_instances(msgs, FunctionCallOutputItem)
    assert not agent.pending_function_calls
    assert client.calls == 1
