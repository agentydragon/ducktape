"""Function learning eval using Microsoft Agent Framework's `Agent.run()`.

The model plays a function-learning game: each turn it queries one input of a
secret boolean function and submits a Python program guess. The scaffold
evaluates the program against all 2^N inputs in a Docker container and reports
Hamming loss.

`Agent.run()` drives the tool-dispatch loop. JSONL transcript writes are
delegated to `JsonlTranscriptProvider` (AF's standard `HistoryProvider`-shaped
audit log via `Message.to_json()`); termination on `game.finished` uses
`terminate_when`. A small `_TokenUsageTracker` ChatMiddleware aggregates
Anthropic prompt-cache read/create token counts that aren't carried on
individual `Message` objects.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import aiodocker
from agent_framework import (
    Agent,
    AgentSession,
    BaseChatClient,
    ChatContext,
    ChatMiddleware,
    ChatResponse,
    FunctionTool,
    MCPStdioTool,
    Message,
    MiddlewareTermination,
)
from skills.eval_infra.empty_skill.empty_skill_skill_spec import SPEC as EMPTY_SKILL_SPEC
from skills.info_gathering.info_gathering_skill_spec import SPEC as INFO_GATHERING_SKILL_SPEC

from skills.eval_infra.af_chat_client import build_model_client
from skills.eval_infra.eval_sandbox import eval_sandbox
from skills.eval_infra.skill_staging import SKILL_FILES_PATH, SkillSpec, stage_skill
from skills.eval_infra.termination import terminate_when
from skills.eval_infra.transcript import JsonlTranscriptProvider
from skills.info_gathering.evals.function_learning.functions import FUNCTIONS, SecretFunction
from skills.info_gathering.evals.function_learning.prompts import build_system_prompt, first_user_message
from skills.info_gathering.evals.function_learning.result_types import (
    FunctionLearningResult,
    RunSummary,
    TokenUsage,
    TurnResult,
)
from skills.info_gathering.evals.function_learning.scoring import EVAL_TIMEOUT_S, evaluate_program
from skills.info_gathering.evals.twenty_questions.x.shared.output import run_output_paths

logger = logging.getLogger(__name__)

_MAX_STEPS = 200

# Maps the --skill CLI value to a SkillSpec. The "off" arm uses an empty
# SKILL.md so the sandbox shape is uniform across arms — there is no
# `if skill_on:` branch.
SKILL_BY_ARM: dict[str, SkillSpec] = {"on": INFO_GATHERING_SKILL_SPEC, "off": EMPTY_SKILL_SPEC}


# --- Game state ---


@dataclass
class GameContext:
    turn_limit: int
    turn: int = 0
    solved: bool = False
    turn_results: list[TurnResult] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0

    @property
    def finished(self) -> bool:
        return self.turn >= self.turn_limit or self.solved


# --- Tools ---


def _make_play_turn_tool(
    game: GameContext, secret_fn: SecretFunction, scoring_container: aiodocker.docker.DockerContainer
) -> FunctionTool:
    async def play_turn(query: int, program: str) -> str:
        """Query the secret function on one input and submit a program guess.

        Args:
            query: Integer in [0, max_input] to evaluate f on.
            program: Python program that takes no input and prints one line per
                     input (0 to max_input), each a decimal integer in [0, max_output].
        """
        if not (0 <= query <= secret_fn.max_input):
            return json.dumps({"error": f"query must be in [0, {secret_fn.max_input}], got {query}"})

        game.turn += 1
        query_result = secret_fn.evaluate(query)
        logger.info("Turn %d: query=%d -> %d", game.turn, query, query_result)

        scoring = await evaluate_program(scoring_container, program, secret_fn)
        score = scoring.score

        if score.hamming_loss == 0:
            game.solved = True

        game.turn_results.append(TurnResult(turn=game.turn, query=query, query_result=query_result, score=score))
        logger.info("Turn %d: hamming_loss=%d (eval %.1fs)", game.turn, score.hamming_loss, scoring.total_eval_s)

        response: dict[str, object] = {
            "turn": game.turn,
            "turns_remaining": game.turn_limit - game.turn,
            "query_result": f"f({query}) = {query_result}",
            "hamming_loss": score.hamming_loss,
            "total_possible_loss": (secret_fn.max_input + 1) * secret_fn.m,
        }
        if game.solved:
            response["game_over"] = "Solved! Your program is correct for all inputs."
        if score.has_errors:
            response["error_summary"] = {
                "parse_errors": score.parse_errors,
                "out_of_range": score.out_of_range,
                "missing_lines": score.missing_lines,
            }
        if score.examples:
            response["error_examples"] = [{"line": e.line, "error": e.error} for e in score.examples]

        return json.dumps(response, indent=2)

    return FunctionTool(
        name="play_turn",
        description=(
            "Query the secret function on one input and submit your program guess. "
            "The program takes no input and prints one output per line for inputs 0..max_input."
        ),
        func=play_turn,
    )


# --- Middleware ---


class _TokenUsageTracker(ChatMiddleware):
    """Aggregate token usage (incl. Anthropic prompt-cache reads/creations)
    that AF doesn't carry on individual `Message` objects."""

    def __init__(self, game: GameContext) -> None:
        self._game = game

    async def process(self, context: ChatContext, call_next: Any) -> None:
        await call_next()
        if not isinstance(context.result, ChatResponse):
            return  # streaming path; not used here

        usage = context.result.usage_details
        if usage:
            self._game.total_input_tokens += usage.get("input_token_count") or 0
            self._game.total_output_tokens += usage.get("output_token_count") or 0
            # The two Anthropic-specific keys aren't declared on the UsageDetails
            # TypedDict, so `.get()` is typed `object | None`; cast for the sum.
            self._game.total_cache_read_tokens += cast(int, usage.get("anthropic.cache_read_input_tokens") or 0)
            self._game.total_cache_creation_tokens += cast(int, usage.get("anthropic.cache_creation_input_tokens") or 0)


# --- Main ---


_NO_HINT = "The function class is unknown. You must discover its structure from queries alone."


async def run_game(
    *,
    function_name: str,
    hint: bool,
    turn_limit: int,
    model: str,
    api: str,
    output_dir: Path,
    exec_tool: MCPStdioTool,
    scoring_container: aiodocker.docker.DockerContainer,
    model_client: BaseChatClient[Any],
    skill_md: str,
) -> RunSummary:
    """Execute one function learning game and persist results.

    The caller owns `model_client`'s lifecycle; this function neither
    constructs nor closes it. The caller also owns staging the skill
    (extracting the tar, mounting the dir into `exec_tool`'s container at
    `SKILL_FILES_PATH`); `skill_md` is the SKILL.md text to inline (empty
    string for the off-arm — the empty-skill tar has an empty SKILL.md).
    """
    secret_fn = FUNCTIONS[function_name]
    description = secret_fn.description if hint else _NO_HINT

    system = build_system_prompt(skill=skill_md, has_scratch=True, skill_files_path=SKILL_FILES_PATH)
    opening = first_user_message(secret_fn, turn_limit, description, eval_timeout_s=EVAL_TIMEOUT_S)

    calls_path, summary_path = run_output_paths(f"fl_{function_name}_{'hint' if hint else 'nohint'}", output_dir)

    with calls_path.open("w") as log_f:
        # AF "instructions" don't flow through the Message stream — seed it
        # manually so the JSONL transcript has the full context at the top.
        log_f.write(Message("system", [system]).to_json() + "\n")
        log_f.flush()

        game = GameContext(turn_limit=turn_limit)
        play_turn_tool = _make_play_turn_tool(game, secret_fn, scoring_container)

        agent = Agent(
            client=model_client,
            instructions=system,
            tools=[play_turn_tool, exec_tool],
            context_providers=[JsonlTranscriptProvider(log_f)],
            middleware=[_TokenUsageTracker(game), terminate_when(lambda: game.finished, reason="game finished")],
            default_options={"tool_choice": "required", "allow_multiple_tool_calls": False},
        )

        # `terminate_when` raises `MiddlewareTermination` once `game.finished`.
        with contextlib.suppress(MiddlewareTermination):
            await agent.run(opening, session=AgentSession())

    per_turn = [tr.score.hamming_loss for tr in game.turn_results]
    per_turn += [0] * (turn_limit - len(per_turn))
    total_hamming = sum(per_turn)

    fl_result = FunctionLearningResult(
        total_hamming_loss=total_hamming, per_turn_losses=per_turn, solved_at_turn=game.turn if game.solved else None
    )

    summary = RunSummary(
        eval_name=function_name,
        framework="agent_framework",
        model=model,
        api=api,
        turns=game.turn,
        result=fl_result,
        function_name=secret_fn.name,
        n_bits=secret_fn.n,
        m_bits=secret_fn.m,
        usage=TokenUsage(
            input_tokens=game.total_input_tokens,
            output_tokens=game.total_output_tokens,
            cache_read_input_tokens=game.total_cache_read_tokens,
            cache_creation_input_tokens=game.total_cache_creation_tokens,
        ),
    )
    summary_path.write_text(summary.model_dump_json(indent=2))
    logger.info("Saved results to %s", summary_path.parent)
    return summary


async def _async_main(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir) if args.output_dir else Path("eval_results") / "function_learning"
    output_dir.mkdir(parents=True, exist_ok=True)

    model_client = build_model_client(
        api=args.api, model=args.model, function_invocation_configuration={"max_iterations": _MAX_STEPS}
    )

    staged = stage_skill(SKILL_BY_ARM[args.skill], output_dir / "skill_extract")

    async with eval_sandbox(skill=staged) as exec_tool:
        container_name = f"fl-scoring-{uuid.uuid4().hex[:8]}"
        async with aiodocker.Docker() as docker:
            container = await docker.containers.run(
                config={"Image": "python:3.13-slim", "Cmd": ["sleep", "3600"]}, name=container_name
            )
            try:
                summary = await run_game(
                    function_name=args.function,
                    hint=not args.no_hint,
                    model=args.model,
                    api=args.api,
                    output_dir=output_dir,
                    exec_tool=exec_tool,
                    scoring_container=container,
                    model_client=model_client,
                    skill_md=staged.md_text,
                    turn_limit=args.turn_limit,
                )
            finally:
                await container.delete(force=True)
    logger.info("Result: %s", summary.result.model_dump_json())


def main() -> None:
    parser = argparse.ArgumentParser(description="Function Learning Eval — Agent Framework")
    parser.add_argument("--function", choices=list(FUNCTIONS), required=True)
    parser.add_argument("--no-hint", action="store_true")
    parser.add_argument("--model", required=True)
    parser.add_argument("--api", choices=["openai", "anthropic"], default="anthropic")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--skill",
        choices=["on", "off"],
        default="on",
        help="Skill arm: 'on' mounts info_gathering, 'off' mounts an empty skill tar.",
    )
    parser.add_argument("--turn-limit", type=int, required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
