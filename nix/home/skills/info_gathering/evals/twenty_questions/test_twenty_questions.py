"""Tests for the 20 Questions eval game loop."""

from unittest.mock import MagicMock

import anthropic.types
import pytest_bazel

from nix.home.skills.info_gathering.evals.twenty_questions.twenty_questions import (
    Correct,
    Timeout,
    run_twenty_questions,
)


def _text_response(text: str) -> anthropic.types.Message:
    """Build a mock agent response (text only, stop=end_turn)."""
    return anthropic.types.Message(
        id="msg_test",
        content=[anthropic.types.TextBlock(type="text", text=text)],
        model="test-model",
        role="assistant",
        stop_reason="end_turn",
        type="message",
        usage=anthropic.types.Usage(input_tokens=10, output_tokens=20),
    )


def _tool_response(tool_name: str, tool_input: dict, tool_id: str) -> anthropic.types.Message:
    """Build a mock sim response (single tool call, stop=tool_use)."""
    return anthropic.types.Message(
        id="msg_test",
        content=[anthropic.types.ToolUseBlock(type="tool_use", id=tool_id, name=tool_name, input=tool_input)],
        model="test-model",
        role="assistant",
        stop_reason="tool_use",
        type="message",
        usage=anthropic.types.Usage(input_tokens=10, output_tokens=20),
    )


def test_timeout(tmp_path):
    """3-turn game with no correct guess -> Timeout, score=0."""
    client = MagicMock()
    client.messages.create.side_effect = [
        _text_response("Is it alive?"),
        _tool_response("answer", {"response": "no"}, "tu_1"),
        _text_response("Is it man-made?"),
        _tool_response("answer", {"response": "yes"}, "tu_2"),
        _text_response("Is it a tool?"),
        _tool_response("answer", {"response": "no"}, "tu_3"),
    ]

    summary = run_twenty_questions(
        name="test_timeout",
        client=client,
        model="test-model",
        agent_system="You are a helpful assistant.",
        first_user_message="Play 20 Questions. I'm thinking of a thing.",
        sim_system="The secret is: wrench",
        turn_limit=3,
        output_dir=tmp_path,
    )

    assert isinstance(summary.result, Timeout)
    assert summary.result.limit == 3
    assert summary.turns == 3
    assert client.messages.create.call_count == 6


def test_success_on_turn_2(tmp_path):
    """Agent guesses correctly on turn 2 -> Correct(turns=2)."""
    client = MagicMock()
    client.messages.create.side_effect = [
        _text_response("Is it a US state?"),
        _tool_response("answer", {"response": "yes"}, "tu_1"),
        _text_response("My answer is: New Mexico"),
        _tool_response("correct_answer", {}, "tu_2"),
    ]

    summary = run_twenty_questions(
        name="test_success",
        client=client,
        model="test-model",
        agent_system="You are a helpful assistant.",
        first_user_message="Play 20 Questions. I'm thinking of a US state.",
        sim_system="The secret is: New Mexico",
        turn_limit=10,
        output_dir=tmp_path,
    )

    assert isinstance(summary.result, Correct)
    assert summary.result.turns == 2
    assert summary.turns == 2
    assert client.messages.create.call_count == 4


def test_success_on_turn_1(tmp_path):
    """Agent guesses correctly on turn 1 -> Correct(turns=1)."""
    client = MagicMock()
    client.messages.create.side_effect = [
        _text_response("My answer is: sourdough starter"),
        _tool_response("correct_answer", {}, "tu_1"),
    ]

    summary = run_twenty_questions(
        name="test_lucky",
        client=client,
        model="test-model",
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
