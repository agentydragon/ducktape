"""Ollama smoke test: verify text completion and tool calling via Responses API.

Tagged 'ollama'. The mock target runs in CI; the live target requires an Ollama
instance or LiteLLM proxy with OPENAI_API_KEY and OPENAI_BASE_URL set.
"""

from __future__ import annotations

import logging

import pytest_bazel
from pydantic import BaseModel, ConfigDict

from agent_core.agent import Agent
from agent_core.direct_provider import DirectToolProvider
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.logging_handler import LoggingHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.testing.responses import DecoratorMock
from agent_core.turn_limit import MaxTurnsHandler
from openai_utils.model import UserMessage

logger = logging.getLogger(__name__)


class CapitalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str


async def test_text_completion(mock_or_live, recording_handler) -> None:
    """Agent produces a text response to a simple question."""
    provider = DirectToolProvider()

    @mock_or_live(DecoratorMock)
    def client(m: DecoratorMock):
        yield
        yield m.assistant_text("The capital of France is Paris.")

    agent = await Agent.create(
        tool_provider=provider,
        client=client,
        handlers=[
            FinishOnTextMessageHandler(),
            MaxTurnsHandler(max_turns=5),
            LoggingHandler(logger),
            recording_handler,
        ],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("What is the capital of France?"))

    res = await agent.run()

    assert res.text, "Agent produced no text response"
    print(f"Response: {res.text}")


async def test_tool_calling(mock_or_live, recording_handler) -> None:
    """Agent calls a tool and uses the result in its response."""
    provider = DirectToolProvider()

    @provider.tool
    def lookup_capital(args: CapitalInput) -> str:
        """Look up the capital city of a country."""
        capitals = {"france": "Paris", "germany": "Berlin", "japan": "Tokyo"}
        return capitals.get(args.country.lower(), "Unknown")

    @mock_or_live(DecoratorMock)
    def client(m: DecoratorMock):
        yield
        yield m.tool_call("lookup_capital", {"country": "France"})
        yield m.assistant_text("The capital of France is Paris.")

    agent = await Agent.create(
        tool_provider=provider,
        client=client,
        handlers=[
            FinishOnTextMessageHandler(),
            MaxTurnsHandler(max_turns=5),
            LoggingHandler(logger),
            recording_handler,
        ],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("Use the lookup_capital tool to find the capital of France."))

    res = await agent.run()

    assert res.text, "Agent produced no text response"
    print(f"Response: {res.text}")


if __name__ == "__main__":
    pytest_bazel.main()
