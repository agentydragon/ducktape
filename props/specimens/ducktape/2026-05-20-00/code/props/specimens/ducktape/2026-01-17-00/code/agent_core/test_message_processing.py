"""Tests for message processing, forwarding, reasoning threading, and compaction."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from hamcrest import assert_that, has_item, has_properties, instance_of, not_

from agent_core.agent import Agent
from agent_core.compaction import CompactionHandler
from agent_core.events import AssistantText, GroundTruthUsage, Response, SystemText, UserText
from agent_core.handler import BaseHandler, FinishOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage, Compact, NoAction, RequireAnyTool
from agent_core_testing.echo_server import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME, EchoInput
from agent_core_testing.matchers import assert_items_exclude_instance, assert_items_include_instances
from agent_core_testing.responses import DecoratorMock, EchoMock
from mcp_infra.naming import build_mcp_function
from openai_utils.model import (
    AssistantMessage,
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    OutputText,
    ReasoningItem,
    SystemMessage,
    UserMessage,
)

# --- Process message event tests ---


async def test_process_message_fires_system_text_event(noop_agent, recording_handler) -> None:
    """Test that process_message fires on_system_text_event for SystemMessage."""
    noop_agent.process_message(SystemMessage.text("System prompt content"))

    assert len(recording_handler.text_events) == 1
    assert isinstance(recording_handler.text_events[0], SystemText)
    assert recording_handler.text_events[0].text == "System prompt content"


async def test_process_message_fires_user_text_event(noop_agent, recording_handler) -> None:
    """Test that process_message fires on_user_text_event for UserMessage."""
    noop_agent.process_message(UserMessage.text("User says hello"))

    assert len(recording_handler.text_events) == 1
    assert isinstance(recording_handler.text_events[0], UserText)
    assert recording_handler.text_events[0].text == "User says hello"


async def test_process_message_fires_assistant_text_event(noop_agent, recording_handler) -> None:
    """Test that process_message fires on_assistant_text_event for AssistantMessage."""
    noop_agent.process_message(AssistantMessage.text("Assistant response"))

    assert len(recording_handler.text_events) == 1
    assert isinstance(recording_handler.text_events[0], AssistantText)
    assert recording_handler.text_events[0].text == "Assistant response"


async def test_process_message_adds_to_transcript(noop_agent, recording_handler) -> None:
    """Test that process_message adds messages to transcript."""
    noop_agent.process_message(SystemMessage.text("Sys"))
    noop_agent.process_message(UserMessage.text("Usr"))

    assert len(noop_agent._transcript) == 2
    assert isinstance(noop_agent._transcript[0], SystemMessage)
    assert isinstance(noop_agent._transcript[1], UserMessage)
    # Both should have fired events
    assert len(recording_handler.text_events) == 2


# --- Message forwarding tests ---


@pytest.mark.timeout(1)
async def test_stateless_reasoning_forwarding(mcp_client_echo) -> None:
    """Request1 produces reasoning+assistant; Request2 should include reasoning in input."""

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        yield
        yield [m.make_item_reasoning(), m.assistant_text("ok")]

    agent = await Agent.create(
        mcp_client=mcp_client_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("say hi"))
    await agent.run()

    assert_items_include_instances(agent.to_openai_messages(), ReasoningItem, AssistantMessage)


@pytest.mark.timeout(1)
async def test_function_call_and_function_call_output_replay(mcp_client_echo) -> None:
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
        mcp_client=mcp_client_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("say hi"))
    await agent.run()


@pytest.mark.timeout(1)
async def test_mixed_reasoning_fc_ordering(mcp_client_echo) -> None:
    """Resp1 returns reasoning, function_call, assistant; after function_call_output, messages preserves order
    reasoning, function_call, function_call_output, assistant.
    """

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        # Single response with reasoning + tool call + text - agent finishes immediately
        yield [m.make_item_reasoning(), m.echo_call("hi"), m.assistant_text("done")]

    agent = await Agent.create(
        mcp_client=mcp_client_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("start"))
    await agent.run()

    messages = agent.to_openai_messages()
    assert_items_include_instances(messages, ReasoningItem, FunctionCallItem, FunctionCallOutputItem, AssistantMessage)


@pytest.mark.timeout(1)
async def test_no_synthesized_reasoning_items(mcp_client_echo) -> None:
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
        mcp_client=mcp_client_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("say hi"))
    await agent.run()


# --- Reasoning threading tests ---


async def test_reasoning_threading_filters_reasoning_from_next_input(mcp_client_echo) -> None:
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
        assert_that(turn2_input, has_item(instance_of(FunctionCallOutputItem) & has_properties(call_id="call_1")))

        # Turn 3: should include both turns' sequences
        req3 = yield [m.make_item_reasoning(id="rs_turn2"), fc2]
        turn3_input = list(req3.input or [])
        # Turn 1's sequence still intact
        assert_that(turn3_input, has_item(has_properties(id="rs_turn1")))
        assert_that(turn3_input, has_item(has_properties(call_id="call_1", id="fc_id_1")))
        assert_that(turn3_input, has_item(instance_of(FunctionCallOutputItem) & has_properties(call_id="call_1")))
        # Turn 2's sequence
        assert_that(turn3_input, has_item(has_properties(id="rs_turn2")))
        assert_that(turn3_input, has_item(has_properties(call_id="call_2", id="fc_id_2", status="in_progress")))
        assert_that(turn3_input, has_item(instance_of(FunctionCallOutputItem) & has_properties(call_id="call_2")))

        yield m.assistant_text("done")

    agent = await Agent.create(
        mcp_client=mcp_client_echo,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("say hi"))

    res = await agent.run()
    assert res.text.strip() == "done"


# --- Compaction tests ---


class MockOpenAIClient:
    """Mock OpenAI client for testing."""

    def __init__(self):
        self.model = "gpt-4o-mini-test"
        self.responses_create = AsyncMock()

    async def setup_summary_response(self, summary_text: str):
        """Configure the mock to return a specific summary."""
        mock_response = Mock()
        mock_response.id = "test-response-id"
        mock_response.usage = None
        mock_response.output = [AssistantMessageOut(parts=[OutputText(text=summary_text)])]
        self.responses_create.return_value = mock_response


@pytest.fixture
def mock_openai():
    """Create a mock OpenAI client for testing."""
    return MockOpenAIClient()


async def test_compact_transcript_basic(compositor_client, mock_openai):
    """Test basic transcript compaction."""
    await mock_openai.setup_summary_response("User asked about compaction. Assistant explained the concept.")

    agent = await Agent.create(
        mcp_client=compositor_client, client=mock_openai, handlers=[BaseHandler()], tool_policy=RequireAnyTool()
    )
    agent.process_message(SystemMessage.text("Test system prompt"))

    # Add some conversation history to the transcript
    agent._transcript.extend(
        [
            UserMessage.text("What is compaction?"),
            AssistantMessage.text("Compaction is a technique for managing limited context windows."),
            UserMessage.text("How does it work?"),
            AssistantMessage.text("It summarizes old messages to save tokens."),
            UserMessage.text("Can you give an example?"),
            AssistantMessage.text("Sure, here is an example..."),
        ]
    )

    # Compact with keep_recent_turns=2 (should keep last 2 exchanges)
    result = await agent.compact_transcript(keep_recent_turns=2)

    # Verify compaction happened
    assert result.compacted

    # Verify transcript structure
    # Should have: [summary, last 2 user/assistant pairs]
    assert len(agent._transcript) == 3  # summary + 2 recent messages
    # Type narrow to check content
    item0 = agent._transcript[0]
    assert isinstance(item0, UserMessage | AssistantMessage)
    assert item0.content is not None
    # Check that the summary text is present (no longer wrapped in tags)
    assert "User asked about compaction" in item0.content[0].text
    item1 = agent._transcript[1]
    assert isinstance(item1, UserMessage | AssistantMessage)
    assert item1.content is not None
    assert item1.content[0].text == "Can you give an example?"
    item2 = agent._transcript[2]
    assert isinstance(item2, UserMessage | AssistantMessage)
    assert item2.content is not None
    assert item2.content[0].text == "Sure, here is an example..."

    # Verify summarization was called
    mock_openai.responses_create.assert_called_once()


async def test_compact_transcript_insufficient_history(compositor_client, mock_openai):
    """Test that compaction doesn't happen when history is too short."""
    agent = await Agent.create(
        mcp_client=compositor_client, client=mock_openai, handlers=[BaseHandler()], tool_policy=RequireAnyTool()
    )
    agent.process_message(SystemMessage.text("Test system prompt"))

    # Add only a few messages
    agent._transcript.extend([UserMessage.text("Hello"), AssistantMessage.text("Hi there")])

    # Try to compact with keep_recent_turns=10 (more than we have)
    result = await agent.compact_transcript(keep_recent_turns=10)

    # Verify compaction didn't happen
    assert not result.compacted

    # Transcript unchanged
    assert len(agent._transcript) == 3  # System + User + Assistant messages

    # No summarization call
    mock_openai.responses_create.assert_not_called()


async def test_compaction_handler_triggers_at_threshold(compositor_client, mock_openai):
    """Test that CompactionHandler tracks tokens and returns Compact decision when threshold exceeded."""
    # Create compaction handler with low threshold
    handler = CompactionHandler(threshold_tokens=1000, keep_recent_turns=2)

    # Simulate token usage below threshold
    handler.on_response(
        Response(
            response_id="test-id", usage=GroundTruthUsage(model="gpt-4o-mini", total_tokens=500), model="gpt-4o-mini"
        )
    )
    decision = handler.on_before_sample()
    assert isinstance(decision, NoAction)  # Not ready to compact yet

    # Simulate token usage exceeding threshold
    handler.on_response(
        Response(
            response_id="test-id2", usage=GroundTruthUsage(model="gpt-4o-mini", total_tokens=600), model="gpt-4o-mini"
        )
    )
    decision = handler.on_before_sample()
    assert isinstance(decision, Compact)  # Should trigger compaction
    assert decision.keep_recent_turns == 2

    # Simulate successful compaction (resets token counter)
    handler.on_compaction_complete(compacted=True)

    # After successful compaction, should return NoAction
    decision = handler.on_before_sample()
    assert isinstance(decision, NoAction)


async def test_compaction_handler_integrated_with_agent(compositor_client, mock_openai):
    """Test that CompactionHandler works when integrated with agent loop."""
    await mock_openai.setup_summary_response("Summary of early conversation.")

    # Create agent with compaction handler (low threshold for testing)
    handler = CompactionHandler(threshold_tokens=100, keep_recent_turns=2)

    agent = await Agent.create(
        mcp_client=compositor_client, client=mock_openai, handlers=[handler], tool_policy=RequireAnyTool()
    )
    agent.process_message(SystemMessage.text("Test system prompt"))

    # Add conversation history
    agent._transcript.extend(
        [
            UserMessage.text("First message"),
            AssistantMessage.text("First response"),
            UserMessage.text("Second message"),
            AssistantMessage.text("Second response"),
            UserMessage.text("Third message"),
            AssistantMessage.text("Third response"),
        ]
    )

    # Trigger compaction by simulating token usage
    handler.on_response(
        Response(
            response_id="test-response",
            usage=GroundTruthUsage(model="gpt-4o-mini", total_tokens=150),
            model="gpt-4o-mini",
        )
    )

    # Manually trigger compaction (in real agent loop, _run_one_phase would do this)
    decision = handler.on_before_sample()
    assert isinstance(decision, Compact)
    await agent.compact_transcript(keep_recent_turns=decision.keep_recent_turns)

    # Verify transcript was compacted
    assert len(agent._transcript) < 6  # Original was 6 messages
    # Check that summary text is present (no longer wrapped in tags)
    assert any("Summary of early conversation" in str(item) for item in agent._transcript)
