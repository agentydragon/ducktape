"""Tests for the Twenty Questions LangGraph implementation."""

from unittest.mock import AsyncMock

import pytest_bazel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from skills.info_gathering.evals.twenty_questions.result_types import Correct, Timeout
from skills.info_gathering.evals.twenty_questions.x.langgraph.twenty_questions import GameState, build_graph


class _ExecInput(BaseModel):
    cmd: list[str] = Field(description="Command array passed directly to exec (no shell)")
    cwd: str | None = Field(default=None, description="Working directory inside container")
    timeout_ms: int = Field(default=30000, description="Timeout in milliseconds")


def _text_response(text: str) -> AIMessage:
    return AIMessage(content=text)


def _tool_call_response(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def _make_mock_model(responses: list[AIMessage]) -> AsyncMock:
    mock = AsyncMock()
    mock.ainvoke = AsyncMock(side_effect=list(responses))
    mock.bind_tools = lambda tools, **kwargs: mock
    return mock


def _initial_state(*, turn_limit: int, sim_system: str = "The secret is: test") -> GameState:
    return {
        "guesser_messages": [SystemMessage(content="You are a guesser."), HumanMessage(content="Play 20 Questions.")],
        "simulator_messages": [SystemMessage(content=sim_system)],
        "turn": 1,
        "turn_limit": turn_limit,
        "result": None,
        "last_question": None,
        "log_entries": [],
    }


async def test_correct_on_turn_2():
    """Agent guesses correctly on turn 2."""
    guesser_model = _make_mock_model([_text_response("Is it a state?"), _text_response("My answer is: New Mexico")])
    sim_model = _make_mock_model(
        [_tool_call_response("answer", {"response": "yes"}), _tool_call_response("correct_answer", {})]
    )

    graph = build_graph(guesser_model=guesser_model, simulator_model=sim_model)
    app = graph.compile()

    final = await app.ainvoke(_initial_state(turn_limit=10, sim_system="The secret is: New Mexico"))

    assert final["result"] == Correct(turns=2)
    assert guesser_model.ainvoke.call_count == 2
    assert sim_model.ainvoke.call_count == 2


async def test_timeout():
    """3-turn game with no correct guess produces Timeout."""
    guesser_model = _make_mock_model(
        [_text_response("Is it alive?"), _text_response("Is it big?"), _text_response("Is it a tool?")]
    )
    sim_model = _make_mock_model(
        [
            _tool_call_response("answer", {"response": "no"}),
            _tool_call_response("answer", {"response": "yes"}),
            _tool_call_response("answer", {"response": "no"}),
        ]
    )

    graph = build_graph(guesser_model=guesser_model, simulator_model=sim_model)
    app = graph.compile()

    final = await app.ainvoke(_initial_state(turn_limit=3, sim_system="The secret is: wrench"))

    assert final["result"] == Timeout(limit=3)
    assert guesser_model.ainvoke.call_count == 3
    assert sim_model.ainvoke.call_count == 3


async def test_correct_on_turn_1():
    """Agent guesses correctly on turn 1."""
    guesser_model = _make_mock_model([_text_response("My answer is: sourdough starter")])
    sim_model = _make_mock_model([_tool_call_response("correct_answer", {})])

    graph = build_graph(guesser_model=guesser_model, simulator_model=sim_model)
    app = graph.compile()

    final = await app.ainvoke(_initial_state(turn_limit=10, sim_system="The secret is: sourdough starter"))

    assert final["result"] == Correct(turns=1)
    assert guesser_model.ainvoke.call_count == 1
    assert sim_model.ainvoke.call_count == 1


async def test_log_entries_recorded():
    """Log entries are produced for each guesser and simulator turn."""
    guesser_model = _make_mock_model([_text_response("Is it red?")])
    sim_model = _make_mock_model([_tool_call_response("correct_answer", {})])

    graph = build_graph(guesser_model=guesser_model, simulator_model=sim_model)
    app = graph.compile()

    final = await app.ainvoke(_initial_state(turn_limit=10, sim_system="The secret is: rose"))
    entries = final["log_entries"]

    assert len(entries) == 2
    assert entries[0].player == "guesser"
    assert entries[0].content == "Is it red?"
    assert entries[1].player == "simulator"
    assert entries[1].tool_calls == [{"name": "correct_answer", "args": {}}]


def _make_mock_exec_tool() -> StructuredTool:
    """Create a mock exec tool that returns a fixed response."""

    async def _exec(cmd: list[str], cwd: str | None = None, timeout_ms: int = 30000) -> str:
        return f"exec result for: {' '.join(cmd)}"

    return StructuredTool.from_function(
        coroutine=_exec,
        name="exec",
        description="Run a command inside a scratch Docker container.",
        args_schema=_ExecInput,
    )


async def test_exec_tool_routes_through_exec_node():
    """Guesser calls exec tool, gets result, then asks a question."""
    guesser_model = _make_mock_model(
        [
            # First call: guesser decides to use exec tool
            AIMessage(
                content="",
                tool_calls=[{"name": "exec", "args": {"cmd": ["cat", "/etc/os-release"]}, "id": "call_exec_1"}],
            ),
            # Second call: after seeing exec result, guesser asks a question
            _text_response("Is it a US state?"),
        ]
    )
    sim_model = _make_mock_model([_tool_call_response("correct_answer", {})])

    exec_tool = _make_mock_exec_tool()
    graph = build_graph(guesser_model=guesser_model, simulator_model=sim_model, exec_tool=exec_tool)
    app = graph.compile()

    final = await app.ainvoke(_initial_state(turn_limit=10))

    assert final["result"] == Correct(turns=1)
    # Guesser invoked twice: once for exec tool call, once for the actual question.
    assert guesser_model.ainvoke.call_count == 2
    assert sim_model.ainvoke.call_count == 1


async def test_exec_tool_multiple_calls_before_question():
    """Guesser calls exec multiple times before producing a question."""
    guesser_model = _make_mock_model(
        [
            AIMessage(content="", tool_calls=[{"name": "exec", "args": {"cmd": ["whoami"]}, "id": "call_exec_1"}]),
            AIMessage(content="", tool_calls=[{"name": "exec", "args": {"cmd": ["ls", "/"]}, "id": "call_exec_2"}]),
            _text_response("Is it alive?"),
        ]
    )
    sim_model = _make_mock_model([_tool_call_response("answer", {"response": "no"})])

    exec_tool = _make_mock_exec_tool()
    graph = build_graph(guesser_model=guesser_model, simulator_model=sim_model, exec_tool=exec_tool)
    app = graph.compile()

    final = await app.ainvoke(_initial_state(turn_limit=1))

    # turn_limit=1, so after 1 question + answer, the game ends with Timeout
    assert final["result"] == Timeout(limit=1)
    assert guesser_model.ainvoke.call_count == 3
    assert sim_model.ainvoke.call_count == 1


if __name__ == "__main__":
    pytest_bazel.main()
