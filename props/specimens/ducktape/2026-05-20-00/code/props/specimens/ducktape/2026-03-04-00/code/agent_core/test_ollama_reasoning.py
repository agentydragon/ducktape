"""Ollama-specific test: verify reasoning items are parsed from thinking models.

Run manually against a local Ollama instance with a thinking model (e.g., gpt-oss).
Tagged 'manual' + 'ollama' so it never runs in CI.

Ollama returns reasoning via the OpenAI-compatible Responses API in `summary` and
leaves `content` empty:

    ReasoningItem(
        id="rs_...",
        summary=[ReasoningSummaryItem(text="<chain-of-thought text>", type="summary_text")],
        content=[],  # always empty — Ollama puts everything in summary
    )
"""

from __future__ import annotations

import logging

import pytest_bazel

from agent_core.agent import Agent
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.logging_handler import LoggingHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.testing.mcp.responses import EchoMock
from agent_core.turn_limit import MaxTurnsHandler
from openai_utils.model import ReasoningItem, UserMessage

logger = logging.getLogger(__name__)


async def test_reasoning_elicited(mock_or_live, mcp_tool_provider_echo, recording_handler) -> None:
    """Agent handles reasoning items from a thinking model without error."""

    @mock_or_live(EchoMock)
    def client(m: EchoMock):
        yield
        yield [m.make_item_reasoning(), m.assistant_text("$1,504")]

    agent = await Agent.create(
        tool_provider=mcp_tool_provider_echo,
        client=client,
        handlers=[
            FinishOnTextMessageHandler(),
            MaxTurnsHandler(max_turns=5),
            LoggingHandler(logger),
            recording_handler,
        ],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(
        UserMessage.text(
            "A farmer has 3 fields. Field A produces 12 bushels/acre across 15 acres. "
            "Field B produces 8 bushels/acre across 22 acres. Field C produces 15 bushels/acre "
            "across 10 acres. He sells wheat at $7/bushel but pays $3/bushel in costs. "
            "What is his total profit?"
        )
    )

    res = await agent.run()

    assert res.text, "Agent produced no text response"
    reasoning = [item for item in agent.to_openai_messages() if isinstance(item, ReasoningItem)]
    for r in reasoning:
        print(f"Reasoning: {r}")
    print(f"Answer: {res.text}")
    assert reasoning, "Expected at least one ReasoningItem from thinking model"


if __name__ == "__main__":
    pytest_bazel.main()
