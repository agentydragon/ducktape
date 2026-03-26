"""Twenty Questions eval using PydanticAI with structured guesser tools.

The guesser uses ask_yes_no_question and guess_answer tools (which internally
invoke the simulator) plus an optional exec tool for scratch computation.
tool_choice='required' ensures every guesser response is a tool call.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.result import RunContext
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.fastmcp import FastMCPToolset
from pydantic_ai.toolsets.function import FunctionToolset

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


# -- Simulator output types --


class SimAnswer(BaseModel):
    """The simulator answers a yes/no question."""

    response: Literal["yes", "no", "sort_of"]


class SimCorrectAnswer(BaseModel):
    """The simulator confirms the guesser's answer is correct."""


class SimInvalidInput(BaseModel):
    """The simulator rejects invalid input."""

    reason: str


SimAction = SimAnswer | SimCorrectAnswer | SimInvalidInput


# -- Agent construction --

guesser_agent: Agent[None, str] = Agent(defer_model_check=True)

sim_agent: Agent[None, SimAction] = Agent(
    defer_model_check=True, output_type=[SimAnswer, SimCorrectAnswer, SimInvalidInput]
)


# -- Game state --


@dataclass
class GameState:
    turn_limit: int
    turn: int = 0
    result: Result | None = None
    invalid_input_count: int = 0
    log_entries: list[LogEntry] = field(default_factory=list)
    sim_history: list[ModelMessage] | None = None

    def record(
        self, player: Literal["guesser", "simulator"], content: str, tool_calls: list[dict[str, object]] | None = None
    ) -> None:
        self.log_entries.append(
            LogEntry(timestamp=datetime.now(UTC), player=player, content=content, tool_calls=tool_calls or [])
        )


def _make_model_id(api: str, model: str) -> str:
    if api == "openai":
        return f"openai:{model}"
    return f"anthropic:{model}"


def _build_game_toolset(*, state: GameState, model_id: str | None, sim_instructions: str) -> FunctionToolset[None]:
    """Build a FunctionToolset with ask_yes_no_question and guess_answer tools."""
    toolset: FunctionToolset[None] = FunctionToolset()

    async def _run_sim(text: str) -> SimAction:
        """Invoke the simulator for one turn."""
        sim_run = await sim_agent.run(
            text, model=model_id, message_history=state.sim_history, instructions=sim_instructions
        )
        state.sim_history = sim_run.all_messages()
        return sim_run.output

    @toolset.tool
    async def ask_yes_no_question(ctx: RunContext[None], /, question: str) -> str:
        """Ask a yes/no question. Uses one turn."""
        state.turn += 1
        state.record("guesser", question, [{"name": "ask_yes_no_question", "args": {"question": question}}])
        logger.info("Guesser (turn %d): %s", state.turn, question[:200])

        action = await _run_sim(f"Question: {question}")

        if isinstance(action, SimInvalidInput):
            state.invalid_input_count += 1
            state.record("simulator", action.reason, [{"name": "invalid_input", "args": {"reason": action.reason}}])
            state.turn -= 1  # Refund the turn.
            return action.reason

        if isinstance(action, SimAnswer):
            state.record("simulator", action.response, [{"name": "answer", "args": {"response": action.response}}])
            logger.info("Simulator: %s", action.response)
            if state.turn >= state.turn_limit and state.result is None:
                state.result = Timeout(limit=state.turn_limit)
            return action.response

        if isinstance(action, SimCorrectAnswer):
            state.record("simulator", "", [{"name": "correct_answer", "args": {}}])
            state.result = Correct(turns=state.turn)
            logger.info("Correct answer on turn %d!", state.turn)
            return "Correct! You guessed it!"

        return "error"

    @toolset.tool
    async def guess_answer(ctx: RunContext[None], /, answer: str) -> str:
        """Guess the secret answer. Uses one turn."""
        state.turn += 1
        state.record("guesser", answer, [{"name": "guess_answer", "args": {"answer": answer}}])
        logger.info("Guesser (turn %d) guesses: %s", state.turn, answer[:200])

        action = await _run_sim(f"My answer is: {answer}")

        if isinstance(action, SimCorrectAnswer):
            state.record("simulator", "", [{"name": "correct_answer", "args": {}}])
            state.result = Correct(turns=state.turn)
            logger.info("Correct answer on turn %d!", state.turn)
            return "Correct! You guessed it!"

        if isinstance(action, SimAnswer):
            state.record("simulator", action.response, [{"name": "answer", "args": {"response": action.response}}])
            logger.info("Simulator: %s", action.response)
            if state.turn >= state.turn_limit and state.result is None:
                state.result = Timeout(limit=state.turn_limit)
            return action.response  # "no" for wrong guess

        if isinstance(action, SimInvalidInput):
            state.invalid_input_count += 1
            state.record("simulator", action.reason, [{"name": "invalid_input", "args": {"reason": action.reason}}])
            state.turn -= 1
            return action.reason

        return "no"

    return toolset


_MAX_RUNS = 200  # Safety cap.


async def run_game_loop(
    *,
    model_id: str | None,
    guesser_instructions: str,
    sim_instructions: str,
    opening: str,
    turn_limit: int,
    guesser_toolsets: list[AbstractToolset[None]] | None = None,
) -> tuple[Result, int, list[LogEntry], int]:
    """Run the game loop, returning (result, turns_played, log_entries, invalid_input_count)."""
    state = GameState(turn_limit=turn_limit)

    game_toolset = _build_game_toolset(state=state, model_id=model_id, sim_instructions=sim_instructions)
    all_toolsets: list[AbstractToolset[None]] = [game_toolset]
    if guesser_toolsets:
        all_toolsets.extend(guesser_toolsets)

    guesser_history: list[ModelMessage] | None = None

    for _ in range(_MAX_RUNS):
        if state.result is not None:
            break

        guesser_run = await guesser_agent.run(
            opening if guesser_history is None else "",
            model=model_id,
            message_history=guesser_history,
            instructions=guesser_instructions,
            toolsets=all_toolsets,
            model_settings=ModelSettings(tool_choice="required"),  # type: ignore[typeddict-unknown-key]
        )
        guesser_history = guesser_run.all_messages()

    if state.result is None:
        state.result = Timeout(limit=turn_limit)

    return state.result, state.turn, state.log_entries, state.invalid_input_count


async def run_twenty_questions(
    *,
    name: str,
    model_id: str,
    model_name: str,
    api: str,
    variant_name: str,
    output_dir: Path,
    exec_server: ContainerExecServer | None = None,
) -> RunSummary:
    variant = VARIANTS[variant_name]
    calls_path, summary_path = run_output_paths(name, output_dir)

    sim_instructions = load_sim_prompt(secret=variant.secret, turn_limit=variant.turn_limit)
    guesser_instructions = build_guesser_system(skill=load_skill_prompt(), has_scratch=True)
    opening = first_user_message(variant.domain_description, variant.turn_limit)

    guesser_toolsets: list[AbstractToolset[None]] | None = None
    if exec_server is not None:
        guesser_toolsets = [FastMCPToolset(exec_server)]

    result, turn, log_entries, invalid_input_count = await run_game_loop(
        model_id=model_id,
        guesser_instructions=guesser_instructions,
        sim_instructions=sim_instructions,
        opening=opening,
        turn_limit=variant.turn_limit,
        guesser_toolsets=guesser_toolsets,
    )

    with calls_path.open("w") as f:
        for entry in log_entries:
            f.write(entry.model_dump_json() + "\n")

    summary = RunSummary(
        eval_name=name,
        framework="pydantic_ai",
        model=model_name,
        api=api,
        turns=turn,
        result=result,
        invalid_input_count=invalid_input_count,
    )
    save_summary(summary=summary, summary_path=summary_path)
    return summary


async def _async_main(args: argparse.Namespace) -> None:
    name = f"20q_{args.variant}"
    output_dir = output_dir_from_args(args)
    model_id = _make_model_id(args.api, args.model)

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  %s (pydantic_ai)", name, args.model, args.api)
    logger.info("=" * 60)

    async with scratch_exec_server() as exec_server:
        summary = await run_twenty_questions(
            name=name,
            model_id=model_id,
            model_name=args.model,
            api=args.api,
            variant_name=args.variant,
            output_dir=output_dir,
            exec_server=exec_server,
        )
    logger.info("%s", summary)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Twenty Questions eval (PydanticAI)")
    add_common_args(p)
    args = p.parse_args()
    resolve_args(args)

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
