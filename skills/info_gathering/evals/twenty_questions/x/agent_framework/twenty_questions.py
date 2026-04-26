"""Twenty Questions game using Microsoft Agent Framework's `Agent.run()`.

Two `Agent` instances share one chat client:

- The simulator agent owns tools `answer`, `correct_answer`, `invalid_input`;
  `tool_choice="required"` forces it to call exactly one per turn. A simulator-end
  middleware raises `MiddlewareTermination` after each tool dispatch, so each
  `sim_agent.run(...)` call resolves to one tool call (no further LLM round-trip).
- The guesser agent owns `ask_yes_no_question`, `guess_answer`, and an optional
  `exec` tool. The game-tool bodies invoke the simulator inline. A game-end
  middleware terminates the loop once `game.result` is set.

Both agents share an `InMemoryHistoryProvider` (auto-attached when no other
HistoryProvider is configured). Per-agent history lives in its own
`AgentSession` so the simulator's transcript doesn't leak into the guesser's
context window.
"""

import argparse
import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agent_framework import (
    Agent,
    AgentSession,
    BaseChatClient,
    FunctionInvocationContext,
    FunctionMiddleware,
    FunctionTool,
    MCPStdioTool,
    MiddlewareTermination,
)
from skills.eval_infra.empty_skill.empty_skill_skill_spec import SPEC as EMPTY_SKILL_SPEC
from skills.info_gathering.info_gathering_skill_spec import SPEC as INFO_GATHERING_SKILL_SPEC

from skills.eval_infra.af_chat_client import build_model_client
from skills.eval_infra.eval_sandbox import SKILL_PATH, eval_sandbox
from skills.eval_infra.skill_staging import SkillSpec, stage_skill
from skills.eval_infra.termination import terminate_when
from skills.info_gathering.evals.twenty_questions.prompts import (
    build_guesser_system,
    first_user_message,
    load_sim_prompt,
)
from skills.info_gathering.evals.twenty_questions.result_types import Correct, LogEntry, Result, RunSummary, Timeout
from skills.info_gathering.evals.twenty_questions.x.shared.cli import (
    add_common_args,
    output_dir_from_args,
    resolve_args,
)
from skills.info_gathering.evals.twenty_questions.x.shared.output import run_output_paths, save_summary
from skills.info_gathering.evals.twenty_questions.x.shared.variants import VARIANTS

logger = logging.getLogger(__name__)

_MAX_STEPS = 200

# Maps the --skill CLI value to a SkillSpec. The "off" arm uses an empty
# SKILL.md so the sandbox shape is uniform across arms — there is no
# `if skill_on:` branch.
_SKILL_BY_ARM: dict[str, SkillSpec] = {"on": INFO_GATHERING_SKILL_SPEC, "off": EMPTY_SKILL_SPEC}


# -- Game state --


@dataclass
class GameContext:
    """Mutable game state. Shared via closures (single-threaded)."""

    turn_limit: int
    turn: int = 0
    result: Result | None = None
    invalid_input_count: int = 0
    log_entries: list[LogEntry] = field(default_factory=list)
    last_sim_response: str = ""

    def record(
        self, player: Literal["guesser", "simulator"], content: str, tool_calls: list[dict[str, object]] | None = None
    ) -> None:
        self.log_entries.append(
            LogEntry(timestamp=datetime.now(UTC), player=player, content=content, tool_calls=tool_calls or [])
        )


# -- Simulator tools --


def _make_sim_tools(game: GameContext) -> list[FunctionTool]:
    """Simulator's three terminal tools. Each updates game state and stores its
    return value on game.last_sim_response so the guesser's tool body can read
    it after `sim_agent.run(...)` returns."""

    async def answer(response: Literal["yes", "no", "sort_of"]) -> str:
        game.record("simulator", response, [{"name": "answer", "args": {"response": response}}])
        game.last_sim_response = response
        logger.info("Simulator: %s", response)
        return response

    async def correct_answer() -> str:
        game.result = Correct(turns=game.turn)
        game.record("simulator", "", [{"name": "correct_answer", "args": {}}])
        game.last_sim_response = "correct"
        logger.info("Correct answer on turn %d!", game.turn)
        return "correct"

    async def invalid_input(reason: str) -> str:
        game.invalid_input_count += 1
        game.record("simulator", reason, [{"name": "invalid_input", "args": {"reason": reason}}])
        game.last_sim_response = reason
        logger.info("Simulator: invalid_input — %s", reason)
        return reason

    return [
        FunctionTool(name="answer", description="Answer a yes/no question.", func=answer),
        FunctionTool(name="correct_answer", description="The player guessed correctly.", func=correct_answer),
        FunctionTool(
            name="invalid_input", description="The input is not a valid question or guess.", func=invalid_input
        ),
    ]


# -- Guesser game tools (invoke simulator inline) --


def _make_game_tools(*, game: GameContext, sim_agent: Agent, sim_session: AgentSession) -> list[FunctionTool]:
    async def _drive_simulator(text: str) -> None:
        # Simulator middleware terminates after the single tool call by design.
        with contextlib.suppress(MiddlewareTermination):
            await sim_agent.run(text, session=sim_session)

    async def ask_yes_no_question(question: str) -> str:
        """Ask a yes/no question. Uses one turn."""
        game.turn += 1
        game.record("guesser", question, [{"name": "ask_yes_no_question", "args": {"question": question}}])
        logger.info("Guesser (turn %d): %s", game.turn, question[:200])

        await _drive_simulator(f"Question: {question}")

        # Refund the turn if the simulator flagged the question as invalid.
        if game.log_entries and game.log_entries[-1].tool_calls:
            last_call = game.log_entries[-1].tool_calls[0]
            if last_call.get("name") == "invalid_input":
                game.turn -= 1
                return game.last_sim_response

        if game.turn >= game.turn_limit and game.result is None:
            game.result = Timeout(limit=game.turn_limit)
        return game.last_sim_response

    async def guess_answer(answer: str) -> str:
        """Guess the secret answer. Uses one turn."""
        game.turn += 1
        game.record("guesser", answer, [{"name": "guess_answer", "args": {"answer": answer}}])
        logger.info("Guesser (turn %d) guesses: %s", game.turn, answer[:200])

        await _drive_simulator(f"My answer is: {answer}")

        if game.result is not None:
            return "Correct! You guessed it!"
        if game.turn >= game.turn_limit and game.result is None:
            game.result = Timeout(limit=game.turn_limit)
        return game.last_sim_response

    return [
        FunctionTool(
            name="ask_yes_no_question", description="Ask a yes/no question. Uses one turn.", func=ask_yes_no_question
        ),
        FunctionTool(name="guess_answer", description="Guess the secret answer. Uses one turn.", func=guess_answer),
    ]


# -- Middleware --


class _SimulatorEndMiddleware(FunctionMiddleware):
    """Terminate the simulator's loop after every tool dispatch (one per run)."""

    async def process(self, context: FunctionInvocationContext, call_next: Any) -> None:
        await call_next()
        raise MiddlewareTermination("simulator turn complete")


# -- Main --


async def run_game(
    *,
    variant_name: str,
    model: str,
    api: str,
    output_dir: Path,
    model_client: BaseChatClient[Any],
    skill_md: str,
    exec_tool: MCPStdioTool,
) -> RunSummary:
    """Execute one Twenty Questions game and persist results.

    The caller owns `model_client`'s lifecycle; this function neither
    constructs nor closes it. The caller also owns staging the skill
    (extracting the tar, mounting the dir into `exec_tool`'s container at
    `SKILL_PATH`); `skill_md` is the SKILL.md text to inline (empty
    string for the off-arm — the empty-skill tar has an empty SKILL.md).
    """
    variant = VARIANTS[variant_name]
    sim_system = load_sim_prompt(secret=variant.secret, turn_limit=variant.turn_limit)
    guesser_system = build_guesser_system(skill=skill_md, has_scratch=True, skill_files_path=SKILL_PATH)
    opening = first_user_message(variant.domain_description, variant.turn_limit)

    game = GameContext(turn_limit=variant.turn_limit)

    sim_agent = Agent(
        client=model_client,
        instructions=sim_system,
        tools=_make_sim_tools(game),
        middleware=[_SimulatorEndMiddleware()],
        default_options={"tool_choice": "required", "allow_multiple_tool_calls": False},
    )
    sim_session = AgentSession()

    guesser_tools: list[FunctionTool | MCPStdioTool] = [
        *_make_game_tools(game=game, sim_agent=sim_agent, sim_session=sim_session),
        exec_tool,
    ]

    guesser_agent = Agent(
        client=model_client,
        instructions=guesser_system,
        tools=guesser_tools,
        middleware=[terminate_when(lambda: game.result is not None, reason="game decided")],
        default_options={"tool_choice": "required", "allow_multiple_tool_calls": False},
    )
    guesser_session = AgentSession()

    # Game-end middleware terminates once `game.result` is set.
    with contextlib.suppress(MiddlewareTermination):
        await guesser_agent.run(opening, session=guesser_session)

    if game.result is None:
        game.result = Timeout(limit=game.turn_limit)
    result_val = game.result
    turns = result_val.limit if isinstance(result_val, Timeout) else result_val.turns

    calls_path, summary_path = run_output_paths(f"af_{variant_name}", output_dir)
    with calls_path.open("w") as f:
        for entry in game.log_entries:
            f.write(entry.model_dump_json() + "\n")

    summary = RunSummary(
        eval_name=variant_name,
        framework="agent_framework",
        model=model,
        api=api,
        turns=turns,
        result=result_val,
        invalid_input_count=game.invalid_input_count,
    )
    save_summary(summary=summary, summary_path=summary_path)
    return summary


async def _async_main(args: argparse.Namespace) -> None:
    output_dir = output_dir_from_args(args)
    model_client = build_model_client(
        api=args.api, model=args.model, function_invocation_configuration={"max_iterations": _MAX_STEPS}
    )

    staged = stage_skill(_SKILL_BY_ARM[args.skill], output_dir / "skill_extract")

    async with eval_sandbox(skill=staged, workspace=output_dir / "work", inputs=None) as exec_tool:
        summary = await run_game(
            variant_name=args.variant,
            model=args.model,
            api=args.api,
            output_dir=output_dir,
            model_client=model_client,
            skill_md=staged.md_text,
            exec_tool=exec_tool,
        )
    logger.info("Result: %s", summary.result.model_dump_json())


def main() -> None:
    parser = argparse.ArgumentParser(description="Twenty Questions — Agent Framework")
    add_common_args(parser)
    parser.add_argument(
        "--skill",
        choices=["on", "off"],
        default="on",
        help="Skill arm: 'on' mounts info_gathering, 'off' mounts an empty skill tar.",
    )
    args = parser.parse_args()
    resolve_args(args)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
