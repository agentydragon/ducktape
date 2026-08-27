"""Tests for the Twenty Questions CrewAI implementation."""

from unittest.mock import MagicMock, patch

import pytest_bazel

from skills.info_gathering.evals.twenty_questions.result_types import Correct, Player, Timeout
from skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions import (
    AnswerTool,
    CorrectAnswerTool,
    SimulatorToolState,
    crewai_model_name,
    run_game_loop,
)


@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_simulator_turn")
@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_guesser_turn")
def test_correct_on_turn_2(mock_guesser, mock_sim):
    """Agent guesses correctly on turn 2 -> Correct(turns=2)."""
    mock_guesser.side_effect = ["Is it a state?", "My answer is: New Mexico"]
    mock_sim.side_effect = [{"name": "answer", "args": {"response": "yes"}}, {"name": "correct_answer", "args": {}}]

    guesser_agent = MagicMock()
    sim_agent = MagicMock()

    result, turns, log_entries = run_game_loop(
        guesser=guesser_agent, simulator=sim_agent, first_msg="Play 20 Questions.", turn_limit=10
    )

    assert isinstance(result, Correct)
    assert result.turns == 2
    assert turns == 2
    assert mock_guesser.call_count == 2
    assert mock_sim.call_count == 2
    # 2 guesser + 2 simulator entries
    assert len(log_entries) == 4
    assert log_entries[0].player == Player.GUESSER
    assert log_entries[1].player == Player.SIMULATOR
    assert log_entries[2].player == Player.GUESSER
    assert log_entries[3].player == Player.SIMULATOR


@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_simulator_turn")
@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_guesser_turn")
def test_timeout(mock_guesser, mock_sim):
    """3-turn game with no correct guess -> Timeout."""
    mock_guesser.side_effect = ["Is it alive?", "Is it big?", "Is it a tool?"]
    mock_sim.side_effect = [
        {"name": "answer", "args": {"response": "no"}},
        {"name": "answer", "args": {"response": "yes"}},
        {"name": "answer", "args": {"response": "no"}},
    ]

    guesser_agent = MagicMock()
    sim_agent = MagicMock()

    result, turns, log_entries = run_game_loop(
        guesser=guesser_agent, simulator=sim_agent, first_msg="Play 20 Questions.", turn_limit=3
    )

    assert isinstance(result, Timeout)
    assert result.limit == 3
    assert turns == 3
    assert mock_guesser.call_count == 3
    assert mock_sim.call_count == 3
    assert len(log_entries) == 6


@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_simulator_turn")
@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_guesser_turn")
def test_correct_on_turn_1(mock_guesser, mock_sim):
    """Agent guesses correctly on turn 1 -> Correct(turns=1)."""
    mock_guesser.side_effect = ["My answer is: sourdough starter"]
    mock_sim.side_effect = [{"name": "correct_answer", "args": {}}]

    guesser_agent = MagicMock()
    sim_agent = MagicMock()

    result, turns, _log_entries = run_game_loop(
        guesser=guesser_agent, simulator=sim_agent, first_msg="Play 20 Questions.", turn_limit=10
    )

    assert isinstance(result, Correct)
    assert result.turns == 1
    assert turns == 1
    assert mock_guesser.call_count == 1
    assert mock_sim.call_count == 1


@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_simulator_turn")
@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_guesser_turn")
def test_log_entries_recorded(mock_guesser, mock_sim):
    """Verify that log entries are produced for each guesser and simulator turn."""
    mock_guesser.side_effect = ["Is it red?"]
    mock_sim.side_effect = [{"name": "correct_answer", "args": {}}]

    guesser_agent = MagicMock()
    sim_agent = MagicMock()

    _, _, log_entries = run_game_loop(
        guesser=guesser_agent, simulator=sim_agent, first_msg="Play 20 Questions.", turn_limit=10
    )

    assert len(log_entries) == 2
    assert log_entries[0].player == Player.GUESSER
    assert log_entries[0].content == "Is it red?"
    assert log_entries[1].player == Player.SIMULATOR
    assert log_entries[1].tool_calls == [{"name": "correct_answer", "args": {}}]


@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_simulator_turn")
@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_guesser_turn")
def test_sim_answer_content_in_log(mock_guesser, mock_sim):
    """Simulator 'answer' tool call records the response text in the log entry."""
    mock_guesser.side_effect = ["Is it in the western US?", "My answer is: New Mexico"]
    mock_sim.side_effect = [{"name": "answer", "args": {"response": "yes"}}, {"name": "correct_answer", "args": {}}]

    guesser_agent = MagicMock()
    sim_agent = MagicMock()

    _, _, log_entries = run_game_loop(
        guesser=guesser_agent, simulator=sim_agent, first_msg="Play 20 Questions.", turn_limit=10
    )

    # First simulator entry should have content="yes"
    sim_entry = log_entries[1]
    assert sim_entry.player == Player.SIMULATOR
    assert sim_entry.content == "yes"


@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_simulator_turn")
@patch("skills.info_gathering.evals.twenty_questions.x.crewai.twenty_questions._run_guesser_turn")
def test_sim_returns_none_ends_game(mock_guesser, mock_sim):
    """Simulator returning None (no tool call) ends the game as Timeout."""
    mock_guesser.side_effect = ["Is it alive?"]
    mock_sim.side_effect = [None]

    guesser_agent = MagicMock()
    sim_agent = MagicMock()

    result, turns, log_entries = run_game_loop(
        guesser=guesser_agent, simulator=sim_agent, first_msg="Play 20 Questions.", turn_limit=10
    )

    assert isinstance(result, Timeout)
    assert turns == 1
    # Only the guesser entry -- sim produced no tool call so no sim log entry
    assert len(log_entries) == 1
    assert log_entries[0].player == Player.GUESSER


def test_answer_tool_sets_state():
    """AnswerTool._run sets the SimulatorToolState result."""
    state = SimulatorToolState()
    tool = AnswerTool(state=state)
    result = tool._run(response="yes")

    assert result == "yes"
    assert state.result == {"name": "answer", "args": {"response": "yes"}}


def test_correct_answer_tool_sets_state():
    """CorrectAnswerTool._run sets the SimulatorToolState result."""
    state = SimulatorToolState()
    tool = CorrectAnswerTool(state=state)
    result = tool._run()

    assert result == "correct"
    assert state.result == {"name": "correct_answer", "args": {}}


def test_crewai_model_name_anthropic():
    assert crewai_model_name("anthropic", "claude-sonnet-5") == "anthropic/claude-sonnet-5"


def test_crewai_model_name_openai():
    assert crewai_model_name("openai", "gpt-4o-mini") == "gpt-4o-mini"


if __name__ == "__main__":
    pytest_bazel.main()
