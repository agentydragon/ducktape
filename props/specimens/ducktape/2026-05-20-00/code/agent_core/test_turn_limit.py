"""Tests for turn_limit.py: MaxTurnsHandler."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
import pytest_bazel

from agent_core.agent import Agent
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.loop_control import RequireAnyTool
from agent_core.testing.mcp.responses import EchoMock
from agent_core.turn_limit import MaxTurnsExceededError, MaxTurnsHandler
from openai_utils.model import OpenAIModelProto, UserMessage


def echo_n_times(m: EchoMock, n: int, then_text: str | None) -> Generator[Any, Any]:
    """Generate n echo roundtrips, optionally followed by assistant text."""
    for i in range(n):
        yield from m.echo_roundtrip(f"call{i + 1}")
    if then_text is not None:
        yield m.assistant_text(then_text)


@pytest.fixture
def make_agent_with_turn_limit(mcp_tool_provider_echo):
    """Factory for creating agents with turn limit."""

    async def _make(client: OpenAIModelProto, max_turns: int):
        return await Agent.create(
            tool_provider=mcp_tool_provider_echo,
            client=client,
            handlers=[FinishOnTextMessageHandler(), MaxTurnsHandler(max_turns=max_turns)],
            tool_policy=RequireAnyTool(),
        )

    return _make


async def test_turn_limit_exceeded(make_agent_with_turn_limit) -> None:
    """Test that MaxTurnsHandler raises MaxTurnsExceededError when limit is exceeded."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from echo_n_times(m, 4, None)

    agent = await make_agent_with_turn_limit(mock, max_turns=3)
    agent.process_message(UserMessage.text("keep calling echo"))

    with pytest.raises(MaxTurnsExceededError) as exc_info:
        await agent.run()

    assert "exceeded maximum allowed turns (3)" in str(exc_info.value).lower()
    assert "stuck in a loop" in str(exc_info.value).lower()


async def test_turn_limit_within_bounds(make_agent_with_turn_limit) -> None:
    """Test that agent completes successfully when staying within turn limit."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from echo_n_times(m, 2, "done")

    agent = await make_agent_with_turn_limit(mock, max_turns=5)
    agent.process_message(UserMessage.text("call echo twice"))

    result = await agent.run()
    assert result.text.strip() == "done"


async def test_turn_limit_exactly_at_boundary(make_agent_with_turn_limit) -> None:
    """Test that agent can use exactly max_turns without error."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from echo_n_times(m, 2, "done")

    agent = await make_agent_with_turn_limit(mock, max_turns=3)
    agent.process_message(UserMessage.text("call echo twice"))

    result = await agent.run()
    assert result.text.strip() == "done"


if __name__ == "__main__":
    pytest_bazel.main()
