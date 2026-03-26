"""Twenty Questions eval using the OpenAI Agents SDK with structured guesser tools.

The guesser has tool_choice='required' and uses ask_yes_no_question / guess_answer
tools (which internally invoke the simulator) plus an optional exec tool.
"""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from agents import (
    Agent,
    FunctionTool,
    ModelSettings,
    Runner,
    RunResult,
    Tool,
    ToolCallOutputItem,
    TResponseInputItem,
    function_tool,
)
from fastmcp.client import Client
from mcp.types import TextContent

from mcp_infra.exec.docker.server import ContainerExecServer
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

# Allow exec tool calls before game tool calls within one guesser step.
_GUESSER_MAX_TOOL_ROUNDS = 25
# One tool call (answer/correct_answer/invalid_input) + tool result.
_SIMULATOR_MAX_TURNS = 2

_MAX_GAME_STEPS = 200  # Safety cap.


@dataclass
class GameState:
    turn_limit: int
    turn: int = 0
    result: Correct | Timeout | None = None
    invalid_input_count: int = 0
    log_entries: list[LogEntry] = field(default_factory=list)
    sim_input: list[TResponseInputItem] = field(default_factory=list)

    def record(
        self, player: Literal["guesser", "simulator"], content: str, tool_calls: list[dict[str, object]] | None = None
    ) -> None:
        self.log_entries.append(
            LogEntry(timestamp=datetime.now(UTC), player=player, content=content, tool_calls=tool_calls or [])
        )


def _make_exec_tool(mcp_client: Client) -> FunctionTool:
    @function_tool(name_override="exec")
    async def run_exec(cmd: list[str], cwd: str | None = None, timeout_ms: int = 30000) -> str:
        """Run a command in a scratch Docker container."""
        result = await mcp_client.call_tool("exec", {"cmd": cmd, "cwd": cwd, "timeout_ms": timeout_ms})
        return "\n".join(block.text for block in result.content if isinstance(block, TextContent))

    return run_exec


def _run_sim_and_extract(sim_result: RunResult) -> tuple[str | None, bool, bool, str | None]:
    """Extract sim response from Runner result. Returns (response, is_correct, is_invalid, invalid_reason)."""
    sim_response: str | None = None
    is_correct = False
    is_invalid = False
    invalid_reason: str | None = None
    for item in sim_result.new_items:
        if isinstance(item, ToolCallOutputItem):
            output = str(item.output)
            if output == "Correct!":
                is_correct = True
            elif output.startswith("Answered: "):
                sim_response = output.removeprefix("Answered: ")
            elif output.startswith("Invalid: "):
                is_invalid = True
                invalid_reason = output.removeprefix("Invalid: ")
    return sim_response, is_correct, is_invalid, invalid_reason


async def run_twenty_questions(
    *,
    name: str,
    model: str,
    variant_name: str,
    api: str,
    output_dir: Path,
    exec_server: ContainerExecServer | None = None,
) -> RunSummary:
    variant = VARIANTS[variant_name]
    calls_path, summary_path = run_output_paths(name, output_dir)

    sim_system = load_sim_prompt(secret=variant.secret, turn_limit=variant.turn_limit)
    guesser_system = build_guesser_system(skill=load_skill_prompt(), has_scratch=True)
    opening = first_user_message(variant.domain_description, variant.turn_limit)

    state = GameState(turn_limit=variant.turn_limit)

    # Simulator tools
    @function_tool
    def answer(response: Literal["yes", "no", "sort_of"]) -> str:
        """Answer the player's yes/no question."""
        return f"Answered: {response}"

    @function_tool
    def correct_answer() -> str:
        """The player correctly guessed the secret."""
        return "Correct!"

    @function_tool
    def invalid_input(reason: str) -> str:
        """The input is not a valid question or guess."""
        return f"Invalid: {reason}"

    simulator_agent = Agent(
        name="simulator",
        instructions=sim_system,
        model=model,
        tools=[answer, correct_answer, invalid_input],
        model_settings=ModelSettings(tool_choice="required"),
    )

    async def _run_sim(text: str) -> tuple[str | None, bool, bool, str | None]:
        """Run simulator turn, return (response, is_correct, is_invalid, invalid_reason)."""
        sim_result = await Runner.run(
            simulator_agent, input=[*state.sim_input, {"role": "user", "content": text}], max_turns=_SIMULATOR_MAX_TURNS
        )
        state.sim_input = sim_result.to_input_list()
        return _run_sim_and_extract(sim_result)

    # Guesser game tools — invoke simulator inline
    @function_tool(name_override="ask_yes_no_question")
    async def ask_yes_no_question(question: str) -> str:
        """Ask a yes/no question. Uses one turn."""
        state.turn += 1
        state.record("guesser", question, [{"name": "ask_yes_no_question", "args": {"question": question}}])
        logger.info("Guesser (turn %d): %s", state.turn, question[:200])

        sim_response, is_correct, is_invalid, invalid_reason = await _run_sim(f"Question: {question}")

        if is_invalid:
            state.invalid_input_count += 1
            state.record(
                "simulator", invalid_reason or "", [{"name": "invalid_input", "args": {"reason": invalid_reason}}]
            )
            state.turn -= 1
            return invalid_reason or ""

        if is_correct:
            state.record("simulator", "", [{"name": "correct_answer", "args": {}}])
            state.result = Correct(turns=state.turn)
            return "Correct! You guessed it!"

        if sim_response:
            state.record("simulator", sim_response, [{"name": "answer", "args": {"response": sim_response}}])
            logger.info("Simulator: %s", sim_response)
            if state.turn >= state.turn_limit and state.result is None:
                state.result = Timeout(limit=state.turn_limit)
            return sim_response

        return "error"

    @function_tool(name_override="guess_answer")
    async def guess_answer(answer_text: str) -> str:
        """Guess the secret answer. Uses one turn."""
        state.turn += 1
        state.record("guesser", answer_text, [{"name": "guess_answer", "args": {"answer": answer_text}}])
        logger.info("Guesser (turn %d) guesses: %s", state.turn, answer_text[:200])

        sim_response, is_correct, _, _ = await _run_sim(f"My answer is: {answer_text}")

        if is_correct:
            state.record("simulator", "", [{"name": "correct_answer", "args": {}}])
            state.result = Correct(turns=state.turn)
            return "Correct! You guessed it!"

        if sim_response:
            state.record("simulator", sim_response, [{"name": "answer", "args": {"response": sim_response}}])
            logger.info("Simulator: %s", sim_response)
            if state.turn >= state.turn_limit and state.result is None:
                state.result = Timeout(limit=state.turn_limit)
            return sim_response

        return "no"

    # Build guesser tools
    guesser_tools: list[Tool] = [ask_yes_no_question, guess_answer]
    async with AsyncExitStack() as stack:
        if exec_server is not None:
            mcp_client = await stack.enter_async_context(Client(exec_server))
            guesser_tools.append(_make_exec_tool(mcp_client))

        guesser_agent = Agent(
            name="guesser",
            instructions=guesser_system,
            model=model,
            tools=guesser_tools,
            model_settings=ModelSettings(tool_choice="required"),
        )

        guesser_input: str | list[TResponseInputItem] = opening

        for _ in range(_MAX_GAME_STEPS):
            if state.result is not None:
                break

            guesser_result = await Runner.run(guesser_agent, input=guesser_input, max_turns=_GUESSER_MAX_TOOL_ROUNDS)
            guesser_input = guesser_result.to_input_list()

    if state.result is None:
        state.result = Timeout(limit=variant.turn_limit)

    with calls_path.open("w") as f:
        for entry in state.log_entries:
            f.write(entry.model_dump_json() + "\n")

    summary = RunSummary(
        eval_name=name,
        framework="openai_agents",
        model=model,
        api=api,
        turns=state.turn,
        result=state.result,
        invalid_input_count=state.invalid_input_count,
    )
    save_summary(summary=summary, summary_path=summary_path)
    return summary


async def _async_main(args: argparse.Namespace) -> None:
    name = f"20q_{args.variant}"
    output_dir = output_dir_from_args(args)

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  openai_agents", name, args.model)
    logger.info("=" * 60)

    async with scratch_exec_server() as server:
        summary = await run_twenty_questions(
            name=name,
            model=args.model,
            variant_name=args.variant,
            api=args.api,
            output_dir=output_dir,
            exec_server=server,
        )
    logger.info("%s", summary)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Twenty Questions (OpenAI Agents SDK)")
    add_common_args(p)
    args = p.parse_args()
    resolve_args(args)

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
