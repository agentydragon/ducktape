"""Ollama-specific test: verify reasoning items are parsed from thinking models.

Tagged 'ollama'. The mock target runs in CI; the live target requires an Ollama
instance with a thinking model and OPENAI_API_KEY pointing at it.

Ollama returns reasoning via the OpenAI-compatible Responses API in `summary` and
leaves `content` empty:

    ReasoningItem(
        id="rs_...",
        summary=[ReasoningSummaryItem(text="<chain-of-thought text>", type="summary_text")],
        content=[],  # always empty — Ollama puts everything in summary
    )
"""

from __future__ import annotations

import pytest_bazel

from openai_utils.model import ReasoningItem, UserMessage
from skills.info_gathering.evals.agent_runtime.agent import Agent
from skills.info_gathering.evals.agent_runtime.loop_control import AllowAnyToolOrTextMessage
from skills.info_gathering.evals.agent_runtime.testing.handlers import TextCaptureHandler, run_until_text
from skills.info_gathering.evals.agent_runtime.testing.mcp.responses import EchoMock


async def test_reasoning_elicited(mock_or_live, mcp_tool_provider_echo) -> None:
    """Agent handles reasoning items from a thinking model without error."""

    @mock_or_live(EchoMock)
    def client(m: EchoMock):
        yield
        yield [m.make_item_reasoning(), m.assistant_text("$1,504")]

    capture = TextCaptureHandler()
    agent = Agent(
        tool_provider=mcp_tool_provider_echo, client=client, handlers=[capture], tool_policy=AllowAnyToolOrTextMessage()
    )
    agent.process_message(
        UserMessage.text(
            "A farmer has 3 fields. Field A produces 12 bushels/acre across 15 acres. "
            "Field B produces 8 bushels/acre across 22 acres. Field C produces 15 bushels/acre "
            "across 10 acres. He sells wheat at $7/bushel but pays $3/bushel in costs. "
            "What is his total profit?"
        )
    )

    res = await run_until_text(agent, capture)

    assert res, "Agent produced no text response"
    reasoning = [item for item in agent.to_openai_messages() if isinstance(item, ReasoningItem)]
    for r in reasoning:
        print(f"Reasoning: {r}")
    print(f"Answer: {res}")
    assert reasoning, "Expected at least one ReasoningItem from thinking model"


if __name__ == "__main__":
    pytest_bazel.main()
