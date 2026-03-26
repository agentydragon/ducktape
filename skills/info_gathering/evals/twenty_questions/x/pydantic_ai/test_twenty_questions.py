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


def _guesser_model(responses: list[str]) -> FunctionModel:
    """Return a FunctionModel producing successive text responses."""
    it: Iterator[str] = iter(responses)

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(next(it))])

    return FunctionModel(handler)


def _sim_model(actions: list[tuple[str, dict[str, object]]]) -> FunctionModel:
    """Return a FunctionModel producing successive output-tool calls.

    Each action is ("SimAnswer", {"response": "yes"}) or ("SimCorrectAnswer", {}).
    The handler discovers the output tool name from AgentInfo.output_tools, so it
    stays correct even if PydanticAI changes its naming convention.
    """
    it: Iterator[tuple[str, dict[str, object]]] = iter(actions)

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        type_name, args = next(it)
        # Find the output tool whose name ends with the type name.
        tool = next(t for t in info.output_tools if t.name.endswith(type_name))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=args)])

    return FunctionModel(handler)


async def test_correct_on_turn_2():
    """Agent guesses correctly on turn 2 -> Correct(turns=2)."""
    guesser_fm = _guesser_model(["Is it a state?", "My answer is: New Mexico"])
    sim_fm = _sim_model([("SimAnswer", {"response": "yes"}), ("SimCorrectAnswer", {})])

    with guesser_agent.override(model=guesser_fm), sim_agent.override(model=sim_fm):
        result, turns, log_entries, _invalid_count = await run_game_loop(
            model_id="test",
            guesser_instructions="You are a helpful assistant.",
            sim_instructions="The secret is: New Mexico",
            opening="Play 20 Questions. I'm thinking of a US state.",
            turn_limit=10,
        )

    assert isinstance(result, Correct)
    assert result.turns == 2
    assert turns == 2
    assert len(log_entries) == 4
    assert log_entries[0].player == "guesser"
    assert log_entries[1].player == "simulator"
    assert log_entries[2].player == "guesser"
    assert log_entries[3].player == "simulator"


async def test_timeout():
    """3-turn game with no correct guess -> Timeout."""
    guesser_fm = _guesser_model(["Is it alive?", "Is it big?", "Is it a tool?"])
    sim_fm = _sim_model(
        [("SimAnswer", {"response": "no"}), ("SimAnswer", {"response": "yes"}), ("SimAnswer", {"response": "no"})]
    )

    with guesser_agent.override(model=guesser_fm), sim_agent.override(model=sim_fm):
        result, turns, log_entries, _invalid_count = await run_game_loop(
            model_id="test",
            guesser_instructions="You are a helpful assistant.",
            sim_instructions="The secret is: wrench",
            opening="Play 20 Questions. I'm thinking of a thing.",
            turn_limit=3,
        )

    assert isinstance(result, Timeout)
    assert result.limit == 3
    assert turns == 3
    assert len(log_entries) == 6


async def test_correct_on_turn_1():
    """Agent guesses correctly on turn 1 -> Correct(turns=1)."""
    guesser_fm = _guesser_model(["My answer is: sourdough starter"])
    sim_fm = _sim_model([("SimCorrectAnswer", {})])

    with guesser_agent.override(model=guesser_fm), sim_agent.override(model=sim_fm):
        result, turns, _, _invalid_count = await run_game_loop(
            model_id="test",
            guesser_instructions="test",
            sim_instructions="The secret is: sourdough starter",
            opening="Play 20 Questions.",
            turn_limit=10,
        )

    assert isinstance(result, Correct)
    assert result.turns == 1
    assert turns == 1


async def test_log_entries_contain_tool_calls():
    """Log entries record the structured tool call for each simulator action."""
    guesser_fm = _guesser_model(["Is it red?"])
    sim_fm = _sim_model([("SimCorrectAnswer", {})])

    with guesser_agent.override(model=guesser_fm), sim_agent.override(model=sim_fm):
        _, _, log_entries, _invalid_count = await run_game_loop(
            model_id="test",
            guesser_instructions="test",
            sim_instructions="The secret is: rose",
            opening="Play 20 Questions.",
            turn_limit=10,
        )

    assert len(log_entries) == 2
    assert log_entries[0].player == "guesser"
    assert log_entries[0].content == "Is it red?"
    assert log_entries[1].player == "simulator"
    assert log_entries[1].tool_calls == [{"name": "correct_answer", "args": {}}]


if __name__ == "__main__":
    pytest_bazel.main()
