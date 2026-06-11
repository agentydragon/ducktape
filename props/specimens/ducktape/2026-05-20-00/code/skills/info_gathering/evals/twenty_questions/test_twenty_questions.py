"""Tests for the 20 Questions eval game loop."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest_bazel

from agent_core.direct_provider import DirectToolProvider
from openai_utils.model import AssistantMessageOut, FunctionCallItem, OutputText, ResponsesResult, ResponseUsage
from skills.info_gathering.evals.twenty_questions.twenty_questions import Correct, Timeout, run_twenty_questions


def _make_usage() -> ResponseUsage:
    return ResponseUsage(input_tokens=10, output_tokens=20, total_tokens=30)


def _text_result(text: str) -> ResponsesResult:
    """Build a mock ResponsesResult with a text message."""
    return ResponsesResult(
        id="msg_test", usage=_make_usage(), output=[AssistantMessageOut(content=[OutputText(text=text)])]
    )


def _tool_result(name: str, args: dict, call_id: str) -> ResponsesResult:
    """Build a mock ResponsesResult with a single tool call."""
    return ResponsesResult(
        id="msg_test",
        usage=_make_usage(),
        output=[FunctionCallItem(name=name, arguments=json.dumps(args), call_id=call_id, id=call_id)],
    )


def _make_mock_model(*responses: ResponsesResult) -> MagicMock:
    mock = AsyncMock()
    mock.model = "test-model"
    mock.responses_create = AsyncMock(side_effect=list(responses))
    return mock


async def test_timeout(tmp_path):
    """3-turn game with no correct guess -> Timeout, score=0."""
    mock_model = _make_mock_model(
        _text_result("Is it alive?"),
        _tool_result("answer", {"response": "no"}, "tu_1"),
        _text_result("Is it man-made?"),
        _tool_result("answer", {"response": "yes"}, "tu_2"),
        _text_result("Is it a tool?"),
        _tool_result("answer", {"response": "no"}, "tu_3"),
    )

    summary = await run_twenty_questions(
        name="test_timeout",
        model=mock_model,
        agent_system="You are a helpful assistant.",
        first_user_message="Play 20 Questions. I'm thinking of a thing.",
        sim_system="The secret is: wrench",
        turn_limit=3,
        output_dir=tmp_path,
        agent_tool_provider=DirectToolProvider(),
    )

    assert isinstance(summary.result, Timeout)
    assert summary.result.limit == 3
    assert summary.turns == 3
    assert mock_model.responses_create.call_count == 6


async def test_success_on_turn_2(tmp_path):
    """Agent guesses correctly on turn 2 -> Correct(turns=2)."""
    mock_model = _make_mock_model(
        _text_result("Is it a US state?"),
        _tool_result("answer", {"response": "yes"}, "tu_1"),
        _text_result("My answer is: New Mexico"),
        _tool_result("correct_answer", {}, "tu_2"),
    )

    summary = await run_twenty_questions(
        name="test_success",
        model=mock_model,
        agent_system="You are a helpful assistant.",
        first_user_message="Play 20 Questions. I'm thinking of a US state.",
        sim_system="The secret is: New Mexico",
        turn_limit=10,
        output_dir=tmp_path,
        agent_tool_provider=DirectToolProvider(),
    )

    assert isinstance(summary.result, Correct)
    assert summary.result.turns == 2
    assert summary.turns == 2
    assert mock_model.responses_create.call_count == 4


async def test_success_on_turn_1(tmp_path):
    """Agent guesses correctly on turn 1 -> Correct(turns=1)."""
    mock_model = _make_mock_model(
        _text_result("My answer is: sourdough starter"), _tool_result("correct_answer", {}, "tu_1")
    )

    summary = await run_twenty_questions(
        name="test_lucky",
        model=mock_model,
        agent_system="test",
        first_user_message="Play 20 Questions.",
        sim_system="The secret is: sourdough starter",
        turn_limit=10,
        output_dir=tmp_path,
        agent_tool_provider=DirectToolProvider(),
    )

    assert isinstance(summary.result, Correct)
    assert summary.result.turns == 1
    assert summary.turns == 1


if __name__ == "__main__":
    pytest_bazel.main()
