"""Tests for the Twenty Questions PydanticAI implementation.

Uses FunctionModel to mock only the LLM behavior. The guesser model produces
game tool calls (ask_yes_no_question/guess_answer), then text to end the run().
The simulator model produces output tool calls (SimAnswer/SimCorrectAnswer).
All real game logic, state management, and tool execution runs unmodified.
"""

from collections.abc import Iterator

import pytest_bazel
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from skills.info_gathering.evals.twenty_questions.result_types import Correct, Timeout
from skills.info_gathering.evals.twenty_questions.x.pydantic_ai.twenty_questions import (
    guesser_agent,
    run_game_loop,
    sim_agent,
)

models.ALLOW_MODEL_REQUESTS = False


def _guesser_model(actions: list[tuple[str, dict[str, object]]]) -> FunctionModel:
    """Return a FunctionModel that calls game tools then produces text.

    Each action is a tool call. After each tool call is executed by PydanticAI,
    the model is called again — it produces text to end the run(). The outer
    game loop then calls run() again for the next action.
    """
    it: Iterator[tuple[str, dict[str, object]]] = iter(actions)
    pending_tool: tuple[str, dict[str, object]] | None = None

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal pending_tool
        if pending_tool is not None:
            # Previous tool was executed — produce text to end this run().
            pending_tool = None
            return ModelResponse(parts=[TextPart(content="continuing")])
        try:
            tool_name, args = next(it)
            pending_tool = (tool_name, args)
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        except StopIteration:
            return ModelResponse(parts=[TextPart(content="done")])

    return FunctionModel(handler)


def _sim_model(actions: list[tuple[str, dict[str, object]]]) -> FunctionModel:
    """Return a FunctionModel producing successive output-tool calls."""
    it: Iterator[tuple[str, dict[str, object]]] = iter(actions)

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        type_name, args = next(it)
        tool = next(t for t in info.output_tools if t.name.endswith(type_name))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=args)])

    return FunctionModel(handler)


async def test_correct_on_turn_2():
    """Agent asks a question then guesses correctly on turn 2."""
    guesser_fm = _guesser_model(
        [("ask_yes_no_question", {"question": "Is it a state?"}), ("guess_answer", {"answer": "New Mexico"})]
    )
    sim_fm = _sim_model([("SimAnswer", {"response": "yes"}), ("SimCorrectAnswer", {})])

    with guesser_agent.override(model=guesser_fm), sim_agent.override(model=sim_fm):
        result, turns, _log_entries, _invalid_count = await run_game_loop(
            model_id="test",
            guesser_instructions="You are a helpful assistant.",
            sim_instructions="The secret is: New Mexico",
            opening="Play 20 Questions. I'm thinking of a US state.",
            turn_limit=10,
        )

    assert isinstance(result, Correct)
    assert result.turns == 2
    assert turns == 2


async def test_timeout():
    """3-turn game with no correct guess -> Timeout."""
    guesser_fm = _guesser_model(
        [
            ("ask_yes_no_question", {"question": "Is it alive?"}),
            ("ask_yes_no_question", {"question": "Is it big?"}),
            ("ask_yes_no_question", {"question": "Is it a tool?"}),
        ]
    )
    sim_fm = _sim_model(
        [("SimAnswer", {"response": "no"}), ("SimAnswer", {"response": "yes"}), ("SimAnswer", {"response": "no"})]
    )

    with guesser_agent.override(model=guesser_fm), sim_agent.override(model=sim_fm):
        result, turns, _log_entries, _invalid_count = await run_game_loop(
            model_id="test",
            guesser_instructions="You are a helpful assistant.",
            sim_instructions="The secret is: wrench",
            opening="Play 20 Questions.",
            turn_limit=3,
        )

    assert isinstance(result, Timeout)
    assert result.limit == 3
    assert turns == 3


async def test_correct_on_turn_1():
    """Agent guesses correctly immediately on turn 1."""
    guesser_fm = _guesser_model([("guess_answer", {"answer": "sourdough starter"})])
    sim_fm = _sim_model([("SimCorrectAnswer", {})])

    with guesser_agent.override(model=guesser_fm), sim_agent.override(model=sim_fm):
        result, _turns, _log_entries, _invalid_count = await run_game_loop(
            model_id="test",
            guesser_instructions="You are a helpful assistant.",
            sim_instructions="The secret is: sourdough starter",
            opening="Play 20 Questions.",
            turn_limit=10,
        )

    assert isinstance(result, Correct)
    assert result.turns == 1


async def test_log_entries_contain_tool_calls():
    """Log entries record the structured tool call for the simulator action."""
    guesser_fm = _guesser_model([("ask_yes_no_question", {"question": "Is it red?"})])
    sim_fm = _sim_model([("SimCorrectAnswer", {})])

    with guesser_agent.override(model=guesser_fm), sim_agent.override(model=sim_fm):
        result, _turns, log_entries, _invalid_count = await run_game_loop(
            model_id="test",
            guesser_instructions="You are a helpful assistant.",
            sim_instructions="The secret is: rose",
            opening="Play 20 Questions.",
            turn_limit=10,
        )

    assert isinstance(result, Correct)
    guesser_entries = [e for e in log_entries if e.player == "guesser"]
    sim_entries = [e for e in log_entries if e.player == "simulator"]
    assert len(guesser_entries) >= 1
    assert len(sim_entries) >= 1
    assert sim_entries[0].tool_calls[0]["name"] == "correct_answer"


if __name__ == "__main__":
    pytest_bazel.main()
