"""Tests for the 20 Questions eval game loop."""

import json
from unittest.mock import patch

import litellm
import pytest_bazel

from nix.home.skills.info_gathering.evals.harness import LLMClient
from nix.home.skills.info_gathering.evals.twenty_questions.twenty_questions import (
    Correct,
    Timeout,
    run_twenty_questions,
)


def _text_response(text: str) -> litellm.ModelResponse:
    """Build a mock agent response (text only, stop=stop)."""
    return litellm.ModelResponse(
        id="msg_test",
        choices=[{"finish_reason": "stop", "index": 0, "message": {"content": text, "role": "assistant"}}],
        model="test-model",
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )


def _tool_response(tool_name: str, tool_input: dict, tool_id: str) -> litellm.ModelResponse:
    """Build a mock sim response (single tool call, stop=stop)."""
    return litellm.ModelResponse(
        id="msg_test",
        choices=[
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {
                    "content": None,
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": tool_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": json.dumps(tool_input)},
                        }
                    ],
                },
            }
        ],
        model="test-model",
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )


TEST_CLIENT = LLMClient(model="test-model")


@patch("nix.home.skills.info_gathering.evals.harness.litellm.completion")
def test_timeout(mock_completion, tmp_path):
    """3-turn game with no correct guess -> Timeout, score=0."""
    mock_completion.side_effect = [
        _text_response("Is it alive?"),
        _tool_response("answer", {"response": "no"}, "tu_1"),
        _text_response("Is it man-made?"),
        _tool_response("answer", {"response": "yes"}, "tu_2"),
        _text_response("Is it a tool?"),
        _tool_response("answer", {"response": "no"}, "tu_3"),
    ]

    summary = run_twenty_questions(
        name="test_timeout",
        client=TEST_CLIENT,
        agent_system="You are a helpful assistant.",
        first_user_message="Play 20 Questions. I'm thinking of a thing.",
        sim_system="The secret is: wrench",
        turn_limit=3,
        output_dir=tmp_path,
    )

    assert isinstance(summary.result, Timeout)
    assert summary.result.limit == 3
    assert summary.turns == 3
    assert mock_completion.call_count == 6


@patch("nix.home.skills.info_gathering.evals.harness.litellm.completion")
def test_success_on_turn_2(mock_completion, tmp_path):
    """Agent guesses correctly on turn 2 -> Correct(turns=2)."""
    mock_completion.side_effect = [
        _text_response("Is it a US state?"),
        _tool_response("answer", {"response": "yes"}, "tu_1"),
        _text_response("My answer is: New Mexico"),
        _tool_response("correct_answer", {}, "tu_2"),
    ]

    summary = run_twenty_questions(
        name="test_success",
        client=TEST_CLIENT,
        agent_system="You are a helpful assistant.",
        first_user_message="Play 20 Questions. I'm thinking of a US state.",
        sim_system="The secret is: New Mexico",
        turn_limit=10,
        output_dir=tmp_path,
    )

    assert isinstance(summary.result, Correct)
    assert summary.result.turns == 2
    assert summary.turns == 2
    assert mock_completion.call_count == 4


@patch("nix.home.skills.info_gathering.evals.harness.litellm.completion")
def test_success_on_turn_1(mock_completion, tmp_path):
    """Agent guesses correctly on turn 1 -> Correct(turns=1)."""
    mock_completion.side_effect = [
        _text_response("My answer is: sourdough starter"),
        _tool_response("correct_answer", {}, "tu_1"),
    ]

    summary = run_twenty_questions(
        name="test_lucky",
        client=TEST_CLIENT,
        agent_system="test",
        first_user_message="Play 20 Questions.",
        sim_system="The secret is: sourdough starter",
        turn_limit=10,
        output_dir=tmp_path,
    )

    assert isinstance(summary.result, Correct)
    assert summary.result.turns == 1
    assert summary.turns == 1


if __name__ == "__main__":
    pytest_bazel.main()
