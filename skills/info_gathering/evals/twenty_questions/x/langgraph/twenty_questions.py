"""Twenty Questions eval using LangGraph with structured guesser tools.

The guesser LLM uses tool_choice=required and has three tools:
  - ask_yes_no_question: poses a question, simulator answers yes/no/sort_of
  - guess_answer: makes a guess, simulator confirms or denies
  - exec: optional scratch computation via container

Game tools internally invoke the simulator (a single LLM call) and return the
result string. The game loop calls the guesser repeatedly until a result is set
or the turn limit is reached.

Usage:
  bazel run //skills/info_gathering/evals/twenty_questions/x/langgraph:twenty_questions_bin -- \
    --variant states --api openai --model gpt-4o-mini
"""

import argparse
import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from fastmcp.client import Client
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool, tool as langchain_tool
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from mcp_infra.exec.docker.server import ContainerExecServer
from skills.info_gathering.evals.twenty_questions.prompts import (
    build_guesser_system,
    first_user_message,
    load_sim_prompt,
    load_skill_prompt,
)
from skills.info_gathering.evals.twenty_questions.result_types import Correct, LogEntry, Result, RunSummary, Timeout
from skills.info_gathering.evals.twenty_questions.x.shared.cli import (
    add_common_args,
    output_dir_from_args,
    resolve_args,
)
from skills.info_gathering.evals.twenty_questions.x.shared.docker_exec import scratch_exec_server
from skills.info_gathering.evals.twenty_questions.x.shared.output import run_output_paths, save_summary
from skills.info_gathering.evals.twenty_questions.x.shared.variants import VARIANTS

logger = logging.getLogger(__name__)


# -- Graph state TypedDict (used by build_graph / tests) --


class GameState(TypedDict):
    """LangGraph state dict for the Twenty Questions game."""

    guesser_messages: list[BaseMessage]
    simulator_messages: list[BaseMessage]
    turn: int
    turn_limit: int
    result: Result | None
    last_question: str | None
    log_entries: list[LogEntry]


def build_graph(
    *, guesser_model: BaseChatModel, simulator_model: BaseChatModel, exec_tool: BaseTool | None = None
) -> StateGraph:
    """Build a LangGraph StateGraph for the Twenty Questions game.

    Nodes: guesser (calls the guesser LLM), simulator (calls the simulator LLM),
    exec (runs the exec tool). Edges route based on whether the guesser produced
    a tool call for exec or a text question/guess.
    """
    sim_with_tools = _bind_simulator_tools(simulator_model)

    async def guesser_node(state: GameState) -> dict:
        response: AIMessage = await guesser_model.ainvoke(state["guesser_messages"])
        new_messages = [*state["guesser_messages"], response]
        return {"guesser_messages": new_messages, "last_question": None}

    async def simulator_node(state: GameState) -> dict:
        question_text = state["last_question"] or ""
        turn = state["turn"]
        log_entries = list(state["log_entries"])
        log_entries.append(LogEntry(timestamp=datetime.now(UTC), player="guesser", content=question_text))

        question_msg = HumanMessage(content=question_text)
        sim_messages = [*state["simulator_messages"], question_msg]
        response: AIMessage = await sim_with_tools.ainvoke(sim_messages)
        sim_messages.append(response)

        tool_calls = response.tool_calls or []
        result = state["result"]

        if tool_calls:
            tc = tool_calls[0]
            name = tc["name"]
            args = tc["args"]

            if name == "correct_answer":
                result = Correct(turns=turn)
                log_entries.append(
                    LogEntry(
                        timestamp=datetime.now(UTC),
                        player="simulator",
                        content="correct",
                        tool_calls=[{"name": "correct_answer", "args": {}}],
                    )
                )
            elif name == "answer":
                resp = str(args.get("response", ""))
                log_entries.append(
                    LogEntry(
                        timestamp=datetime.now(UTC),
                        player="simulator",
                        content=resp,
                        tool_calls=[{"name": "answer", "args": {"response": resp}}],
                    )
                )
                new_turn = turn + 1
                if new_turn > state["turn_limit"] and result is None:
                    result = Timeout(limit=state["turn_limit"])
                return {
                    "simulator_messages": sim_messages,
                    "turn": new_turn,
                    "result": result,
                    "log_entries": log_entries,
                    "guesser_messages": [*state["guesser_messages"], HumanMessage(content=resp)],
                }
            elif name == "invalid_input":
                reason = str(args.get("reason", ""))
                log_entries.append(
                    LogEntry(
                        timestamp=datetime.now(UTC),
                        player="simulator",
                        content=reason,
                        tool_calls=[{"name": "invalid_input", "args": {"reason": reason}}],
                    )
                )

        return {"simulator_messages": sim_messages, "result": result, "log_entries": log_entries, "turn": turn}

    async def exec_node(state: GameState) -> dict:
        """Execute the exec tool call and feed the result back to the guesser."""
        assert exec_tool is not None
        messages = state["guesser_messages"]
        last_msg = messages[-1]
        assert isinstance(last_msg, AIMessage)
        tc = last_msg.tool_calls[0]
        result_str = await exec_tool.ainvoke(tc["args"])
        new_messages = [*messages, ToolMessage(content=str(result_str), tool_call_id=tc["id"])]
        return {"guesser_messages": new_messages}

    def route_guesser(state: GameState) -> str:
        """Route after guesser node: exec tool call -> exec, text -> simulator."""
        if state["result"] is not None:
            return END
        messages = state["guesser_messages"]
        last_msg = messages[-1]
        if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
            tc = last_msg.tool_calls[0]
            if tc["name"] == "exec" and exec_tool is not None:
                return "exec"
        # Text response -> extract question and go to simulator
        if isinstance(last_msg, AIMessage):
            content = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
            state["last_question"] = content
        return "simulator"

    def route_simulator(state: GameState) -> str:
        if state["result"] is not None:
            return END
        return "guesser"

    graph = StateGraph(GameState)
    graph.add_node("guesser", guesser_node)
    graph.add_node("simulator", simulator_node)
    if exec_tool is not None:
        graph.add_node("exec", exec_node)
    graph.set_entry_point("guesser")
    graph.add_conditional_edges("guesser", route_guesser)
    graph.add_conditional_edges("simulator", route_simulator)
    if exec_tool is not None:
        graph.add_edge("exec", "guesser")

    return graph


# -- Simulator tool schemas --


class AnswerInput(BaseModel):
    response: Literal["yes", "no", "sort_of"] = Field(description="Answer to the player's yes/no question")


class InvalidInputInput(BaseModel):
    reason: str = Field(description="Why the input is not a valid yes/no question or guess")


# Simulator tools are defined as plain functions; we build LangChain tool objects
# for binding to the simulator model.

SIM_TOOL_SCHEMAS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "answer",
            "description": "Answer the player's yes/no question.",
            "parameters": {
                "type": "object",
                "properties": {"response": {"type": "string", "enum": ["yes", "no", "sort_of"]}},
                "required": ["response"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correct_answer",
            "description": "The player correctly guessed the secret.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "invalid_input",
            "description": "The player's input is not a valid yes/no question or guess.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        },
    },
]


# -- Guesser tool schemas --


class AskYesNoQuestionInput(BaseModel):
    question: str = Field(description="A yes/no question to ask about the secret")


class GuessAnswerInput(BaseModel):
    answer: str = Field(description="Your guess for the secret answer")


# -- Game state (mutable, shared across tool invocations) --


@dataclass
class GameContext:
    """Mutable game state shared between guesser tool implementations."""

    simulator_model: Runnable  # BaseChatModel with bound tools
    simulator_messages: list[BaseMessage]
    turn: int = 1
    turn_limit: int = 20
    result: Result | None = None
    invalid_input_count: int = 0
    log_entries: list[LogEntry] = field(default_factory=list)


# -- Simulator invocation --


async def _invoke_simulator(game: GameContext, player_text: str) -> tuple[str, str]:
    """Send player_text to the simulator LLM and parse the tool call response.

    Returns (tool_name, result_string). Updates game.simulator_messages.
    """
    question_msg = HumanMessage(content=player_text)
    messages = [*game.simulator_messages, question_msg]
    response: AIMessage = await game.simulator_model.ainvoke(messages)

    # Persist the exchange in simulator history.
    game.simulator_messages.extend([question_msg, response])

    tool_calls = response.tool_calls or []
    if not tool_calls:
        logger.warning("Simulator returned no tool calls; treating as invalid_input")
        return "invalid_input", "Simulator failed to use a tool"

    tc = tool_calls[0]
    name = tc["name"]
    args = tc["args"]

    if name == "correct_answer":
        return "correct_answer", "correct"
    if name == "invalid_input":
        return "invalid_input", str(args.get("reason", "invalid input"))
    if name == "answer":
        return "answer", str(args.get("response", ""))

    logger.warning("Simulator called unknown tool %r", name)
    return "invalid_input", f"Unknown simulator tool: {name}"


# -- Game tool implementations --


async def _ask_yes_no_question(game: GameContext, question: str) -> str:
    """Handle the guesser's ask_yes_no_question tool call."""
    guesser_entry = LogEntry(timestamp=datetime.now(UTC), player="guesser", content=question)
    game.log_entries.append(guesser_entry)

    tool_name, result_text = await _invoke_simulator(game, question)

    if tool_name == "invalid_input":
        game.invalid_input_count += 1
        sim_entry = LogEntry(
            timestamp=datetime.now(UTC),
            player="simulator",
            content=result_text,
            tool_calls=[{"name": "invalid_input", "args": {"reason": result_text}}],
        )
        game.log_entries.append(sim_entry)
        return result_text

    if tool_name == "correct_answer":
        game.result = Correct(turns=game.turn)
        sim_entry = LogEntry(
            timestamp=datetime.now(UTC),
            player="simulator",
            content="correct",
            tool_calls=[{"name": "correct_answer", "args": {}}],
        )
        game.log_entries.append(sim_entry)
        return "The simulator says that's correct!"

    # Normal answer — consume a turn.
    sim_entry = LogEntry(
        timestamp=datetime.now(UTC),
        player="simulator",
        content=result_text,
        tool_calls=[{"name": "answer", "args": {"response": result_text}}],
    )
    game.log_entries.append(sim_entry)
    game.turn += 1

    if game.turn > game.turn_limit:
        game.result = Timeout(limit=game.turn_limit)

    return result_text


async def _guess_answer(game: GameContext, answer: str) -> str:
    """Handle the guesser's guess_answer tool call."""
    guesser_entry = LogEntry(timestamp=datetime.now(UTC), player="guesser", content=f"Guess: {answer}")
    game.log_entries.append(guesser_entry)

    tool_name, result_text = await _invoke_simulator(game, f"My guess is: {answer}")

    if tool_name == "correct_answer":
        game.result = Correct(turns=game.turn)
        sim_entry = LogEntry(
            timestamp=datetime.now(UTC),
            player="simulator",
            content="correct",
            tool_calls=[{"name": "correct_answer", "args": {}}],
        )
        game.log_entries.append(sim_entry)
        return "Correct! You guessed it!"

    if tool_name == "invalid_input":
        game.invalid_input_count += 1
        sim_entry = LogEntry(
            timestamp=datetime.now(UTC),
            player="simulator",
            content=result_text,
            tool_calls=[{"name": "invalid_input", "args": {"reason": result_text}}],
        )
        game.log_entries.append(sim_entry)
        return result_text

    # Wrong guess — consume a turn.
    sim_entry = LogEntry(
        timestamp=datetime.now(UTC),
        player="simulator",
        content=result_text,
        tool_calls=[{"name": "answer", "args": {"response": result_text}}],
    )
    game.log_entries.append(sim_entry)
    game.turn += 1

    if game.turn > game.turn_limit:
        game.result = Timeout(limit=game.turn_limit)

    return f"Not correct. The answer was: {result_text}"


# -- Building tools and running the game --


def _make_chat_model(api: str, model: str) -> BaseChatModel:
    if api == "openai":
        return ChatOpenAI(model=model)
    return ChatAnthropic(model=model)


def _build_game_tools(game: GameContext) -> list[BaseTool]:
    """Build LangChain tools that close over the mutable game context."""

    async def ask_yes_no_question(question: str) -> str:
        return await _ask_yes_no_question(game, question)

    async def guess_answer(answer: str) -> str:
        return await _guess_answer(game, answer)

    ask_tool = StructuredTool.from_function(
        coroutine=ask_yes_no_question,
        name="ask_yes_no_question",
        description="Ask a yes/no question about the secret.",
        args_schema=AskYesNoQuestionInput,
    )
    guess_tool = StructuredTool.from_function(
        coroutine=guess_answer,
        name="guess_answer",
        description="Guess the secret answer.",
        args_schema=GuessAnswerInput,
    )
    return [ask_tool, guess_tool]


def _bind_simulator_tools(model: BaseChatModel) -> Runnable:
    """Bind simulator tool schemas so the model always responds with a tool call."""

    @langchain_tool(args_schema=AnswerInput)
    def answer(response: str) -> str:
        """Answer the player's yes/no question."""
        return response

    @langchain_tool
    def correct_answer() -> str:
        """The player correctly guessed the secret."""
        return "correct"

    @langchain_tool(args_schema=InvalidInputInput)
    def invalid_input(reason: str) -> str:
        """The player's input is not a valid yes/no question or guess."""
        return reason

    return model.bind_tools([answer, correct_answer, invalid_input], tool_choice="required")


async def run_twenty_questions_langgraph(
    *,
    name: str,
    api: str,
    model_name: str,
    guesser_system: str,
    sim_system: str,
    first_message: str,
    turn_limit: int,
    output_dir: Path,
    exec_server: ContainerExecServer | None = None,
) -> RunSummary:
    """Run a full Twenty Questions game and return a summary."""
    calls_path, summary_path = run_output_paths(name, output_dir)

    guesser_model = _make_chat_model(api, model_name)
    simulator_model = _bind_simulator_tools(_make_chat_model(api, model_name))

    game = GameContext(
        simulator_model=simulator_model,
        simulator_messages=[SystemMessage(content=sim_system)],
        turn=1,
        turn_limit=turn_limit,
    )

    async with contextlib.AsyncExitStack() as stack:
        # Build game tools (ask/guess) that close over game state.
        game_tools: list[BaseTool] = _build_game_tools(game)

        # Optionally add exec tool from container MCP server.
        if exec_server is not None:
            mcp_client = await stack.enter_async_context(Client(exec_server))
            tools = await load_mcp_tools(mcp_client.session)
            exec_tool = next(t for t in tools if t.name == "exec")
            game_tools.append(exec_tool)

        guesser_with_tools = guesser_model.bind_tools(game_tools, tool_choice="required")

        guesser_messages: list[BaseMessage] = [
            SystemMessage(content=guesser_system),
            HumanMessage(content=first_message),
        ]

        # Game loop: call guesser, execute its tool, feed result back, repeat.
        max_iterations = turn_limit * 3  # Safety bound (accounts for invalid inputs + exec calls).
        for _ in range(max_iterations):
            response: AIMessage = await guesser_with_tools.ainvoke(guesser_messages)
            guesser_messages.append(response)

            tool_calls = response.tool_calls or []
            if not tool_calls:
                logger.warning("Guesser returned no tool calls despite tool_choice=required")
                break

            for tc in tool_calls:
                # Find the matching tool and invoke it.
                matching_tool = next((t for t in game_tools if t.name == tc["name"]), None)
                if matching_tool is None:
                    logger.warning("Guesser called unknown tool %r", tc["name"])
                    continue

                result_str = await matching_tool.ainvoke(tc["args"])

                # Add tool result as a message back to the guesser.
                guesser_messages.append(ToolMessage(content=str(result_str), tool_call_id=tc["id"]))

            if game.result is not None:
                break

    assert game.result is not None, "Game loop terminated without setting result"

    turns = game.turn - 1 if isinstance(game.result, Timeout) else game.turn

    with calls_path.open("w") as f:
        for entry in game.log_entries:
            f.write(entry.model_dump_json() + "\n")

    summary = RunSummary(
        eval_name=name,
        framework="langgraph",
        model=model_name,
        api=api,
        turns=turns,
        result=game.result,
        invalid_input_count=game.invalid_input_count,
    )
    save_summary(summary=summary, summary_path=summary_path)
    return summary


async def _async_main(args: argparse.Namespace) -> None:
    v = VARIANTS[args.variant]
    name = f"20q_{args.variant}"

    guesser_system = build_guesser_system(skill=load_skill_prompt(), has_scratch=True)
    sim_system = load_sim_prompt(secret=v.secret, turn_limit=v.turn_limit)
    first_msg = first_user_message(v.domain_description, v.turn_limit)
    output_dir = output_dir_from_args(args)

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  %s (langgraph)", name, args.model, args.api)
    logger.info("=" * 60)

    async with scratch_exec_server() as exec_server:
        summary = await run_twenty_questions_langgraph(
            name=name,
            api=args.api,
            model_name=args.model,
            guesser_system=guesser_system,
            sim_system=sim_system,
            first_message=first_msg,
            turn_limit=v.turn_limit,
            output_dir=output_dir,
            exec_server=exec_server,
        )
    logger.info("%s", summary)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Twenty Questions eval (LangGraph)")
    add_common_args(p)
    args = p.parse_args()
    resolve_args(args)

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
