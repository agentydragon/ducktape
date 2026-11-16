"""Ollama smoke test: verify text completion and tool calling via Responses API.

Tagged 'ollama'. The mock target runs in CI; the live target requires an Ollama
instance or LiteLLM proxy with OPENAI_API_KEY and OPENAI_BASE_URL set.
"""

from __future__ import annotations

import pytest_bazel
from pydantic import BaseModel, ConfigDict

from openai_utils.model import UserMessage
from openai_utils.testing.responses import DecoratorMock
from skills.info_gathering.evals.agent_runtime.agent import Agent
from skills.info_gathering.evals.agent_runtime.loop_control import AllowAnyToolOrTextMessage
from skills.info_gathering.evals.agent_runtime.testing.handlers import TextCaptureHandler, run_until_text
from skills.info_gathering.evals.twenty_questions.direct_provider import DirectToolProvider


class CapitalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str


async def test_text_completion(mock_or_live) -> None:
    """Agent produces a text response to a simple question."""
    provider = DirectToolProvider()

    @mock_or_live(DecoratorMock)
    def client(m: DecoratorMock):
        yield
        yield m.assistant_text("The capital of France is Paris.")

    capture = TextCaptureHandler()
    agent = Agent(tool_provider=provider, client=client, handlers=[capture], tool_policy=AllowAnyToolOrTextMessage())
    agent.process_message(UserMessage.text("What is the capital of France?"))

    res = await run_until_text(agent, capture)

    assert res, "Agent produced no text response"
    print(f"Response: {res}")


async def test_tool_calling(mock_or_live) -> None:
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

    capture = TextCaptureHandler()
    agent = Agent(tool_provider=provider, client=client, handlers=[capture], tool_policy=AllowAnyToolOrTextMessage())
    agent.process_message(UserMessage.text("Use the lookup_capital tool to find the capital of France."))

    res = await run_until_text(agent, capture)

    assert res, "Agent produced no text response"
    print(f"Response: {res}")


if __name__ == "__main__":
    pytest_bazel.main()
