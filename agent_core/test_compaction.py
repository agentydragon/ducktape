"""Tests for compaction.py: CompactionHandler."""

from __future__ import annotations

import pytest
import pytest_bazel

from agent_core.agent import Agent
from agent_core.compaction import CompactionHandler
from agent_core.events import GroundTruthUsage, Response
from agent_core.handler import BaseHandler
from agent_core.loop_control import Compact, NoAction, RequireAnyTool
from openai_utils.model import (
    AssistantMessage,
    AssistantMessageOut,
    OpenAIModelProto,
    OutputText,
    ResponsesRequest,
    ResponsesResult,
    SystemMessage,
    UserMessage,
)


class SummarizingClient(OpenAIModelProto):
    """Test client that returns a configurable summary response."""

    def __init__(self, summary_text: str):
        self.model = "gpt-4o-mini-test"
        self.summary_text = summary_text
        self.call_count = 0

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        self.call_count += 1
        return ResponsesResult(
            id="test-response-id", usage=None, output=[AssistantMessageOut(parts=[OutputText(text=self.summary_text)])]
        )


@pytest.fixture
def summarizing_client():
    """Create a client that returns a configurable summary."""
    return SummarizingClient(summary_text="User asked about compaction. Assistant explained the concept.")


async def test_compact_transcript_basic(mcp_tool_provider, summarizing_client):
    """Test basic transcript compaction."""
    agent = await Agent.create(
        tool_provider=mcp_tool_provider,
        client=summarizing_client,
        handlers=[BaseHandler()],
        tool_policy=RequireAnyTool(),
    )

    # Build up conversation history via public API
    agent.process_message(SystemMessage.text("Test system prompt"))
    agent.process_message(UserMessage.text("What is compaction?"))
    agent.process_message(AssistantMessage.text("Compaction is a technique for managing limited context windows."))
    agent.process_message(UserMessage.text("How does it work?"))
    agent.process_message(AssistantMessage.text("It summarizes old messages to save tokens."))
    agent.process_message(UserMessage.text("Can you give an example?"))
    agent.process_message(AssistantMessage.text("Sure, here is an example..."))

    # Compact with keep_recent_turns=2 (should keep last 2 messages)
    result = await agent.compact_transcript(keep_recent_turns=2)

    assert result.compacted
    assert summarizing_client.call_count == 1

    # After compaction: [summary, last 2 messages]
    assert agent._transcript == [
        UserMessage.text("User asked about compaction. Assistant explained the concept."),
        UserMessage.text("Can you give an example?"),
        AssistantMessage.text("Sure, here is an example..."),
    ]


async def test_compact_transcript_insufficient_history(mcp_tool_provider, summarizing_client):
    """Test that compaction doesn't happen when history is too short."""
    agent = await Agent.create(
        tool_provider=mcp_tool_provider,
        client=summarizing_client,
        handlers=[BaseHandler()],
        tool_policy=RequireAnyTool(),
    )

    # Only a few messages
    agent.process_message(SystemMessage.text("Test system prompt"))
    agent.process_message(UserMessage.text("Hello"))
    agent.process_message(AssistantMessage.text("Hi there"))

    original_transcript = list(agent._transcript)

    # Try to compact with keep_recent_turns=10 (more than we have)
    result = await agent.compact_transcript(keep_recent_turns=10)

    assert not result.compacted
    assert summarizing_client.call_count == 0
    assert agent._transcript == original_transcript


async def test_compaction_handler_triggers_at_threshold(mcp_tool_provider):
    """Test that CompactionHandler tracks tokens and returns Compact decision when threshold exceeded."""
    handler = CompactionHandler(threshold_tokens=1000, keep_recent_turns=2)

    # Simulate token usage below threshold
    handler.on_response(
        Response(
            response_id="test-id", usage=GroundTruthUsage(model="gpt-4o-mini", total_tokens=500), model="gpt-4o-mini"
        )
    )
    assert isinstance(handler.on_before_sample(), NoAction)

    # Simulate token usage exceeding threshold
    handler.on_response(
        Response(
            response_id="test-id2", usage=GroundTruthUsage(model="gpt-4o-mini", total_tokens=600), model="gpt-4o-mini"
        )
    )
    decision = handler.on_before_sample()
    assert isinstance(decision, Compact)
    assert decision.keep_recent_turns == 2

    # Simulate successful compaction (resets token counter)
    handler.on_compaction_complete(compacted=True)

    # After successful compaction, should return NoAction
    assert isinstance(handler.on_before_sample(), NoAction)


async def test_compaction_handler_integrated_with_agent(mcp_tool_provider):
    """Test that CompactionHandler works when integrated with agent loop."""
    client = SummarizingClient(summary_text="Summary of early conversation.")
    handler = CompactionHandler(threshold_tokens=100, keep_recent_turns=2)

    agent = await Agent.create(
        tool_provider=mcp_tool_provider, client=client, handlers=[handler], tool_policy=RequireAnyTool()
    )

    # Build up conversation history
    agent.process_message(SystemMessage.text("Test system prompt"))
    agent.process_message(UserMessage.text("First message"))
    agent.process_message(AssistantMessage.text("First response"))
    agent.process_message(UserMessage.text("Second message"))
    agent.process_message(AssistantMessage.text("Second response"))
    agent.process_message(UserMessage.text("Third message"))
    agent.process_message(AssistantMessage.text("Third response"))

    original_len = len(agent._transcript)

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
    assert len(agent._transcript) < original_len
    # First item is the summary
    assert agent._transcript[0] == UserMessage.text("Summary of early conversation.")


if __name__ == "__main__":
    pytest_bazel.main()
