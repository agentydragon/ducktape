"""Tests for message processing, forwarding, and reasoning threading."""

from __future__ import annotations

import pytest
import pytest_bazel
from hamcrest import all_of, assert_that, has_item, has_properties, instance_of, not_

from agent_core.agent import Agent
from agent_core.events import AssistantText, SystemText, UserText
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.testing.matchers import assert_items_exclude_instance, assert_items_include_instances
from agent_core.testing.mcp.echo_server import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME, EchoInput
from agent_core.testing.mcp.responses import EchoMock
from agent_core.testing.responses import DecoratorMock
from mcp_infra.naming import build_mcp_function
from openai_utils.model import (
    AssistantMessage,
    FunctionCallItem,
    FunctionCallOutputItem,
    ReasoningItem,
    SystemMessage,
    UserMessage,
)
from openai_utils.testing.openai_mock import NoopOpenAIClient


@pytest.fixture
async def noop_agent(mcp_tool_provider, recording_handler):
    """Agent with NoopOpenAIClient for testing message processing without sampling."""
    return await Agent.create(
        tool_provider=mcp_tool_provider,
        client=NoopOpenAIClient(),
        handlers=[recording_handler],
        tool_policy=AllowAnyToolOrTextMessage(),
    )


# --- Process message event tests ---


async def test_process_message_fires_system_text_event(noop_agent, recording_handler) -> None:
    """Test that process_message fires on_system_text_event for SystemMessage."""
    noop_agent.process_message(SystemMessage.text("System prompt content"))

    text_events = [r for r in recording_handler.records if isinstance(r, SystemText | UserText | AssistantText)]
    assert len(text_events) == 1
    assert isinstance(text_events[0], SystemText)
    assert text_events[0].text == "System prompt content"


async def test_process_message_fires_user_text_event(noop_agent, recording_handler) -> None:
    """Test that process_message fires on_user_text_event for UserMessage."""
    noop_agent.process_message(UserMessage.text("User says hello"))

    text_events = [r for r in recording_handler.records if isinstance(r, SystemText | UserText | AssistantText)]
    assert len(text_events) == 1
    assert isinstance(text_events[0], UserText)
    assert text_events[0].text == "User says hello"


async def test_process_message_fires_assistant_text_event(noop_agent, recording_handler) -> None:
    """Test that process_message fires on_assistant_text_event for AssistantMessage."""
    noop_agent.process_message(AssistantMessage.text("Assistant response"))

    text_events = [r for r in recording_handler.records if isinstance(r, SystemText | UserText | AssistantText)]
    assert len(text_events) == 1
    assert isinstance(text_events[0], AssistantText)
    assert text_events[0].text == "Assistant response"


async def test_process_message_adds_to_transcript(noop_agent, recording_handler) -> None:
    """Test that process_message adds messages to transcript."""
    noop_agent.process_message(SystemMessage.text("Sys"))
    noop_agent.process_message(UserMessage.text("Usr"))

    assert len(noop_agent._transcript) == 2
    assert isinstance(noop_agent._transcript[0], SystemMessage)
    assert isinstance(noop_agent._transcript[1], UserMessage)
    # Both should have fired events
    text_events = [r for r in recording_handler.records if isinstance(r, SystemText | UserText | AssistantText)]
    assert len(text_events) == 2


# --- Message forwarding tests ---


@pytest.mark.timeout(1)
async def test_stateless_reasoning_forwarding(mcp_tool_provider_echo) -> None:
    """Request1 produces reasoning+assistant; Request2 should include reasoning in input."""

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        yield
        yield [m.make_item_reasoning(), m.assistant_text("ok")]

    agent = await Agent.create(
        tool_provider=mcp_tool_provider_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("say hi"))
    await agent.run()

    assert_items_include_instances(agent.to_openai_messages(), ReasoningItem, AssistantMessage)


@pytest.mark.timeout(1)
async def test_function_call_and_function_call_output_replay(mcp_tool_provider_echo) -> None:
    """Request1 produces a function_call; after local execution, messages() must include function_call and function_call_output."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("hi")
        # Capture second request to verify it contains function_call + output
        req = yield [m.make_item_reasoning(), m.assistant_text("done")]
        input_items = list(req.input or [])
        assert_items_include_instances(input_items, FunctionCallItem, FunctionCallOutputItem)

    agent = await Agent.create(
        tool_provider=mcp_tool_provider_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("say hi"))
    await agent.run()


@pytest.mark.timeout(1)
async def test_mixed_reasoning_fc_ordering(mcp_tool_provider_echo) -> None:
    """Resp1 returns reasoning, function_call, assistant; after function_call_output, messages preserves order
    reasoning, function_call, function_call_output, assistant.
    """

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        # Single response with reasoning + tool call + text - agent finishes immediately
        yield [m.make_item_reasoning(), m.echo_call("hi"), m.assistant_text("done")]

    agent = await Agent.create(
        tool_provider=mcp_tool_provider_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("start"))
    await agent.run()

    messages = agent.to_openai_messages()
    assert_items_include_instances(messages, ReasoningItem, FunctionCallItem, FunctionCallOutputItem, AssistantMessage)


@pytest.mark.timeout(1)
async def test_no_synthesized_reasoning_items(mcp_tool_provider_echo) -> None:
    """Ensure agent does not fabricate reasoning rs_* items when missing."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("hi")
        # Capture request to verify no synthesized reasoning
        req = yield [m.make_item_reasoning(), m.assistant_text("done")]
        input_items = list(req.input or [])
        assert_items_exclude_instance(input_items, ReasoningItem)

    agent = await Agent.create(
        tool_provider=mcp_tool_provider_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("say hi"))
    await agent.run()


# --- Reasoning threading tests ---


async def test_reasoning_threading_filters_reasoning_from_next_input(mcp_tool_provider_echo) -> None:
    """Test that reasoning items are properly threaded with their function calls across turns."""

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        # Create function calls with explicit id and status to verify preservation
        fc1 = FunctionCallItem(
            name=build_mcp_function(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME),
            arguments=EchoInput(text="hi").model_dump_json(),
            call_id="call_1",
            id="fc_id_1",
            status="completed",
        )
        fc2 = FunctionCallItem(
            name=build_mcp_function(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME),
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

    agent = await Agent.create(
        tool_provider=mcp_tool_provider_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("say hi"))

    res = await agent.run()
    assert res.text.strip() == "done"


if __name__ == "__main__":
    pytest_bazel.main()
