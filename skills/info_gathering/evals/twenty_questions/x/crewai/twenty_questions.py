"""Twenty Questions eval using CrewAI with structured guesser tools.

The guesser has three BaseTool subclasses: AskYesNoQuestionTool, GuessAnswerTool,
and ExecTool. Game tools (ask/guess) internally create a simulator Crew/Task to
get the answer. The outer loop calls the guesser Crew/Task repeatedly until
game over.

Usage:
  bazel run //skills/info_gathering/evals/twenty_questions/x/crewai:twenty_questions_crewai_bin -- \
    --variant states --api openai --model gpt-4o-mini
"""

import argparse
import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from crewai import Agent, Crew, Process, Task
from crewai.tools import BaseTool
from fastmcp.client import Client
from pydantic import BaseModel, Field, PrivateAttr

from skills.info_gathering.evals.twenty_questions.prompts import (
    build_guesser_system,
    first_user_message,
    load_sim_prompt,
    load_skill_prompt,
)
from skills.info_gathering.evals.twenty_questions.result_types import Correct, LogEntry, RunSummary, Timeout
from skills.info_gathering.evals.twenty_questions.x.shared.cli import (
    add_common_args,
    output_dir_from_args,
    resolve_args,
)
from skills.info_gathering.evals.twenty_questions.x.shared.docker_exec import scratch_exec_server
from skills.info_gathering.evals.twenty_questions.x.shared.output import run_output_paths, save_summary
from skills.info_gathering.evals.twenty_questions.x.shared.variants import VARIANTS

logger = logging.getLogger(__name__)

_MAX_GAME_STEPS = 200  # Safety cap.


# -- Game state --


@dataclass
class GameState:
    turn_limit: int
    turn: int = 0
    result: Correct | Timeout | None = None
    invalid_input_count: int = 0
    log_entries: list[LogEntry] = field(default_factory=list)

    def record(
        self, player: Literal["guesser", "simulator"], content: str, tool_calls: list[dict[str, object]] | None = None
    ) -> None:
        self.log_entries.append(
            LogEntry(timestamp=datetime.now(UTC), player=player, content=content, tool_calls=tool_calls or [])
        )


# -- Simulator tools --


class SimulatorToolState:
    """Per-turn mutable state shared between simulator tools."""

    def __init__(self) -> None:
        self.result: dict[str, object] | None = None


class AnswerInput(BaseModel):
    response: Literal["yes", "no", "sort_of"] = Field(description="The answer: 'yes', 'no', or 'sort_of'.")


class AnswerTool(BaseTool):
    """Answer the player's yes/no question."""

    name: str = "answer"
    description: str = "Answer the player's yes/no question. Response must be 'yes', 'no', or 'sort_of'."
    args_schema: type[BaseModel] = AnswerInput

    _state: SimulatorToolState = PrivateAttr()

    def __init__(self, *, state: SimulatorToolState) -> None:
        super().__init__(
            name="answer",
            description="Answer the player's yes/no question. Response must be 'yes', 'no', or 'sort_of'.",
        )
        self._state = state

    def _run(self, response: str) -> str:
        self._state.result = {"name": "answer", "args": {"response": response}}
        return response


class CorrectAnswerTool(BaseTool):
    """Signal that the player correctly guessed the secret."""

    name: str = "correct_answer"
    description: str = "Call this when the player correctly guessed the secret."

    _state: SimulatorToolState = PrivateAttr()

    def __init__(self, *, state: SimulatorToolState) -> None:
        super().__init__(name="correct_answer", description="Call this when the player correctly guessed the secret.")
        self._state = state

    def _run(self) -> str:
        self._state.result = {"name": "correct_answer", "args": {}}
        return "correct"


class InvalidInputTool(BaseTool):
    """Reject invalid input from the player."""

    name: str = "invalid_input"
    description: str = "Call this when the player's input is not a valid yes/no question or guess."

    _state: SimulatorToolState = PrivateAttr()

    def __init__(self, *, state: SimulatorToolState) -> None:
        super().__init__(
            name="invalid_input",
            description="Call this when the player's input is not a valid yes/no question or guess.",
        )
        self._state = state

    def _run(self, reason: str = "") -> str:
        self._state.result = {"name": "invalid_input", "args": {"reason": reason}}
        return f"Invalid: {reason}"


# -- Simulator runner --


def crewai_model_name(api: str, model: str) -> str:
    """Return the model name in CrewAI/LiteLLM format."""
    if api == "anthropic":
        return f"anthropic/{model}"
    return model


def _run_simulator_turn(*, simulator: Agent, text: str) -> dict[str, object] | None:
    """Execute a single simulator turn and return the tool call dict, or None."""
    state = SimulatorToolState()
    tools = [AnswerTool(state=state), CorrectAnswerTool(state=state), InvalidInputTool(state=state)]

    task = Task(
        description=(
            f"The player said: {text}\n\n"
            "You MUST use one of your tools to respond. "
            "Use 'answer' for yes/no questions, 'correct_answer' if the player guessed correctly, "
            "or 'invalid_input' if the input is not a valid question or guess."
        ),
        expected_output="A tool call response",
        agent=simulator,
        tools=tools,
    )
    crew = Crew(agents=[simulator], tasks=[task], process=Process.sequential, verbose=False)
    crew.kickoff()
    return state.result


def _run_guesser_turn(*, guesser: Agent, prompt: str) -> str:
    """Execute a single guesser turn and return the guesser's text output."""
    task = Task(
        description=prompt,
        expected_output="Use your tools to ask a yes/no question or guess the answer.",
        agent=guesser,
    )
    crew = Crew(agents=[guesser], tasks=[task], process=Process.sequential, verbose=False)
    result = crew.kickoff()
    # CrewOutput has .raw but CrewStreamingOutput does not; getattr handles both.
    raw = getattr(result, "raw", None)
    return str(raw).strip() if raw is not None else str(result).strip()


# -- Guesser game tools --


class ExecInput(BaseModel):
    cmd: list[str] = Field(description="Command array (no shell). Use ['sh', '-c', '...'] for shell features.")
    cwd: str | None = Field(default=None, description="Working directory inside container (None = default).")
    timeout_ms: int = Field(default=30000, description="Timeout in milliseconds.")


class ExecTool(BaseTool):
    """Run a command in a scratch Docker container via MCP exec tool."""

    name: str = "exec"
    description: str = "Run a command in a scratch container. cmd is a list of strings (no shell)."
    args_schema: type[BaseModel] = ExecInput

    _mcp_client: Any = PrivateAttr()
    _loop: Any = PrivateAttr()

    def __init__(self, *, mcp_client: Client, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(
            name="exec", description="Run a command in a scratch container. cmd is a list of strings (no shell)."
        )
        self._mcp_client = mcp_client
        self._loop = loop

    def _run(self, cmd: list[str], cwd: str | None = None, timeout_ms: int = 30000) -> str:
        arguments: dict[str, Any] = {"cmd": cmd, "timeout_ms": timeout_ms}
        if cwd is not None:
            arguments["cwd"] = cwd
        future = asyncio.run_coroutine_threadsafe(self._mcp_client.call_tool("exec", arguments), self._loop)
        result = future.result()
        return "\n".join(block.text for block in result.content if hasattr(block, "text"))


# -- Game loop --


def _process_sim_result(state: GameState, tool_result: dict[str, object] | None) -> str:
    """Process a simulator tool result and update game state. Returns response text for guesser."""
    if tool_result is None:
        logger.warning("Simulator produced no tool call on turn %d", state.turn)
        return "error"

    tool_name = tool_result["name"]

    if tool_name == "invalid_input":
        reason = str(tool_result["args"]["reason"])  # type: ignore[index]
        state.invalid_input_count += 1
        state.record("simulator", reason, [{"name": "invalid_input", "args": {"reason": reason}}])
        state.turn -= 1  # Refund the turn.
        return reason

    if tool_name == "correct_answer":
        state.record("simulator", "", [{"name": "correct_answer", "args": {}}])
        state.result = Correct(turns=state.turn)
        logger.info("Correct answer on turn %d!", state.turn)
        return "Correct! You guessed it!"

    if tool_name == "answer":
        response = str(tool_result["args"]["response"])  # type: ignore[index]
        state.record("simulator", response, [{"name": "answer", "args": {"response": response}}])
        logger.info("Simulator: %s", response)
        if state.turn >= state.turn_limit and state.result is None:
            state.result = Timeout(limit=state.turn_limit)
        return response

    return "error"


def run_game_loop(
    *, guesser: Agent, simulator: Agent, first_msg: str, turn_limit: int
) -> tuple[Correct | Timeout, int, list[LogEntry]]:
    """Run the game loop. Returns (result, turns_played, log_entries)."""
    state = GameState(turn_limit=turn_limit)
    guesser_prompt = first_msg

    for _ in range(_MAX_GAME_STEPS):
        if state.result is not None:
            break

        # Guesser turn
        guesser_text = _run_guesser_turn(guesser=guesser, prompt=guesser_prompt)
        state.turn += 1
        state.record("guesser", guesser_text)
        logger.info("Guesser (turn %d): %s", state.turn, guesser_text[:200])

        # Simulator turn
        tool_result = _run_simulator_turn(simulator=simulator, text=guesser_text)
        sim_response = _process_sim_result(state, tool_result)

        if tool_result is None:
            # Simulator failed to produce a tool call -- end game.
            break

        guesser_prompt = sim_response

    final_result: Correct | Timeout = state.result if state.result is not None else Timeout(limit=turn_limit)
    return final_result, state.turn, state.log_entries


def run_twenty_questions_crewai(
    *,
    name: str,
    api: str,
    model_name: str,
    guesser_system: str,
    sim_system: str,
    first_msg: str,
    turn_limit: int,
    output_dir: Path,
    extra_guesser_tools: list[BaseTool] | None = None,
) -> RunSummary:
    """Run a full 20 Questions game with CrewAI and return a summary."""
    calls_path, summary_path = run_output_paths(name, output_dir)
    llm_name = crewai_model_name(api, model_name)

    guesser_tools: list[BaseTool] = list(extra_guesser_tools) if extra_guesser_tools else []

    guesser = Agent(
        role="Guesser",
        goal="Guess the secret by asking yes/no questions",
        backstory=guesser_system,
        llm=llm_name,
        tools=guesser_tools,
        verbose=False,
    )

    simulator = Agent(
        role="Simulator",
        goal="Answer the player's questions honestly using only the provided tools",
        backstory=sim_system,
        llm=llm_name,
        verbose=False,
    )

    result, turns, log_entries = run_game_loop(
        guesser=guesser, simulator=simulator, first_msg=first_msg, turn_limit=turn_limit
    )

    with calls_path.open("w") as f:
        for entry in log_entries:
            f.write(entry.model_dump_json() + "\n")

    summary = RunSummary(
        eval_name=name,
        framework="crewai",
        model=model_name,
        api=api,
        turns=turns,
        result=result,
        invalid_input_count=sum(
            1
            for e in log_entries
            if e.player == "simulator" and any(tc.get("name") == "invalid_input" for tc in e.tool_calls)
        ),
    )
    save_summary(summary=summary, summary_path=summary_path)
    return summary


async def _setup_and_run(loop: asyncio.AbstractEventLoop, ready: threading.Event, state: dict[str, Any]) -> None:
    """Enter async context managers for the MCP server+client on *loop*.

    Signals *ready* once the client is available in *state*, then waits for
    *state["done"]* to be set before tearing down.
    """
    async with scratch_exec_server() as server, Client(server) as mcp_client:
        state["mcp_client"] = mcp_client
        ready.set()
        done_event: asyncio.Event = state["done_event"]
        await done_event.wait()


def _run_with_exec(args: argparse.Namespace) -> None:
    """Set up an MCP exec bridge on a background event loop, then run the game."""
    bg_loop = asyncio.new_event_loop()
    ready = threading.Event()
    done_async = asyncio.Event()
    state: dict[str, Any] = {"done_event": done_async}

    def _run_bg() -> None:
        asyncio.set_event_loop(bg_loop)
        bg_loop.run_until_complete(_setup_and_run(bg_loop, ready, state))

    bg_thread = threading.Thread(target=_run_bg, daemon=True)
    bg_thread.start()
    ready.wait()

    try:
        exec_tool = ExecTool(mcp_client=state["mcp_client"], loop=bg_loop)
        _run_main(args, exec_tool=exec_tool)
    finally:
        bg_loop.call_soon_threadsafe(done_async.set)
        bg_thread.join(timeout=10)
        bg_loop.close()


def _run_main(args: argparse.Namespace, *, exec_tool: ExecTool | None = None) -> None:
    v = VARIANTS[args.variant]
    name = f"20q_{args.variant}"

    guesser_system = build_guesser_system(skill=load_skill_prompt(), has_scratch=True)
    sim_system = load_sim_prompt(secret=v.secret, turn_limit=v.turn_limit)
    first_msg = first_user_message(v.domain_description, v.turn_limit)
    output_dir = output_dir_from_args(args)

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  %s (crewai)", name, args.model, args.api)
    logger.info("=" * 60)

    extra_tools: list[BaseTool] | None = [exec_tool] if exec_tool is not None else None

    summary = run_twenty_questions_crewai(
        name=name,
        api=args.api,
        model_name=args.model,
        guesser_system=guesser_system,
        sim_system=sim_system,
        first_msg=first_msg,
        turn_limit=v.turn_limit,
        output_dir=output_dir,
        extra_guesser_tools=extra_tools,
    )
    logger.info("%s", summary)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Twenty Questions eval (CrewAI)")
    add_common_args(p)
    args = p.parse_args()
    resolve_args(args)

    _run_with_exec(args)


if __name__ == "__main__":
    main()
