"""Tests for message processing, forwarding, and reasoning threading."""

from __future__ import annotations

import pytest
from hamcrest import all_of, assert_that, has_item, has_properties, instance_of, not_

from openai_utils.model import (
    AssistantMessage,
    FunctionCallItem,
    FunctionCallOutputItem,
    ReasoningItem,
    SystemMessage,
    UserMessage,
)
from openai_utils.testing.openai_mock import NoopOpenAIClient
from openai_utils.testing.responses import DecoratorMock
from skills.info_gathering.evals.agent_runtime.agent import Agent
from skills.info_gathering.evals.agent_runtime.loop_control import AllowAnyToolOrTextMessage
from skills.info_gathering.evals.agent_runtime.testing.handlers import TextCaptureHandler, run_until_text
from skills.info_gathering.evals.agent_runtime.testing.matchers import assert_items_include_instances
from skills.info_gathering.evals.agent_runtime.testing.mcp.echo_server import ECHO_TOOL_NAME, EchoInput
from skills.info_gathering.evals.agent_runtime.testing.mcp.responses import EchoMock


@pytest.fixture
async def noop_agent(mcp_tool_provider):
    """Agent with NoopOpenAIClient for testing message processing without sampling."""
    return Agent(
        tool_provider=mcp_tool_provider, client=NoopOpenAIClient(), handlers=[], tool_policy=AllowAnyToolOrTextMessage()
    )


async def test_process_message_adds_to_transcript(noop_agent) -> None:
    noop_agent.process_message(SystemMessage.text("Sys"))
    noop_agent.process_message(UserMessage.text("Usr"))
    messages = noop_agent.to_openai_messages()
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], UserMessage)


# --- Message forwarding tests ---


async def test_mixed_reasoning_fc_ordering(mcp_tool_provider_echo) -> None:
    """Resp1 returns reasoning, function_call, assistant; after function_call_output, messages preserves order
    reasoning, function_call, function_call_output, assistant.
    """

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield [m.make_item_reasoning(), m.echo_call("hi"), m.assistant_text("done")]

    agent = Agent(
        tool_provider=mcp_tool_provider_echo, client=mock, handlers=[], tool_policy=AllowAnyToolOrTextMessage()
    )
    agent.process_message(UserMessage.text("start"))
    await agent.step()

    messages = agent.to_openai_messages()
    assert_items_include_instances(messages, ReasoningItem, FunctionCallItem, FunctionCallOutputItem, AssistantMessage)


# --- Reasoning threading tests ---


async def test_reasoning_threading_filters_reasoning_from_next_input(mcp_tool_provider_echo) -> None:
    """Test that reasoning items are properly threaded with their function calls across turns."""

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        # Create function calls with explicit id and status to verify preservation
        fc1 = FunctionCallItem(
            name=ECHO_TOOL_NAME,
            arguments=EchoInput(text="hi").model_dump_json(),
            call_id="call_1",
            id="fc_id_1",
            status="completed",
        )
        fc2 = FunctionCallItem(
            name=ECHO_TOOL_NAME,
            arguments=EchoInput(text="bye").model_dump_json(),
            call_id="call_2",
            id="fc_id_2",
            status="in_progress",
        )

        # Turn 1: initial request should have user message but no reasoning
        req1 = yield
        turn1_input = list(req1.input or [])
        assert_that(turn1_input, has_item(instance_of(UserMessage)))
        assert_that(turn1_input, not_(has_item(instance_of(ReasoningItem))))

        # Turn 2: should include turn 1's reasoning + tool call + output
        req2 = yield [m.make_item_reasoning(id="rs_turn1"), fc1]
        turn2_input = list(req2.input or [])
        assert_that(turn2_input, has_item(has_properties(id="rs_turn1")))
        assert_that(turn2_input, has_item(has_properties(call_id="call_1", id="fc_id_1", status="completed")))
        assert_that(
            turn2_input, has_item(all_of(instance_of(FunctionCallOutputItem), has_properties(call_id="call_1")))
        )

        # Turn 3: should include both turns' sequences
        req3 = yield [m.make_item_reasoning(id="rs_turn2"), fc2]
        turn3_input = list(req3.input or [])
        # Turn 1's sequence still intact
        assert_that(turn3_input, has_item(has_properties(id="rs_turn1")))
        assert_that(turn3_input, has_item(has_properties(call_id="call_1", id="fc_id_1")))
        assert_that(
            turn3_input, has_item(all_of(instance_of(FunctionCallOutputItem), has_properties(call_id="call_1")))
        )
        # Turn 2's sequence
        assert_that(turn3_input, has_item(has_properties(id="rs_turn2")))
        assert_that(turn3_input, has_item(has_properties(call_id="call_2", id="fc_id_2", status="in_progress")))
        assert_that(
            turn3_input, has_item(all_of(instance_of(FunctionCallOutputItem), has_properties(call_id="call_2")))
        )

        yield m.assistant_text("done")

    capture = TextCaptureHandler()
    agent = Agent(
        tool_provider=mcp_tool_provider_echo, client=mock, handlers=[capture], tool_policy=AllowAnyToolOrTextMessage()
    )
    agent.process_message(UserMessage.text("say hi"))

    res = await run_until_text(agent, capture)
    assert res.strip() == "done"
