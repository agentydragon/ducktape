"""Function learning eval using AutoGen v0.4.

The model plays a function-learning game: each turn it queries one input of a
secret boolean function and submits a Python program guess. The scaffold evaluates
the program against all 2^N inputs in a Docker container and reports Hamming loss.
"""

import argparse
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiodocker
from autogen_core import CancellationToken
from autogen_core.models import (
    AssistantMessage,
    ChatCompletionClient,
    FunctionExecutionResult,
    FunctionExecutionResultMessage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import FunctionTool
from autogen_ext.models.openai import OpenAIChatCompletionClient
from fastmcp.client import Client
from mcp.types import TextContent

from skills.info_gathering.evals.docker_exec import scratch_exec_server
from skills.info_gathering.evals.function_learning.functions import VARIANTS, SecretFunction
from skills.info_gathering.evals.function_learning.prompts import build_system_prompt, first_user_message
from skills.info_gathering.evals.function_learning.result_types import (
    FunctionLearningResult,
    RunSummary,
    TokenUsage,
    TurnResult,
)
from skills.info_gathering.evals.function_learning.scoring import evaluate_program
from skills.info_gathering.evals.prompt_caching import CachedAnthropicClient
from skills.info_gathering.evals.twenty_questions.prompts import load_skill_prompt
from skills.info_gathering.evals.twenty_questions.x.shared.output import run_output_paths

logger = logging.getLogger(__name__)

_MAX_STEPS = 200


@dataclass
class GameContext:
    turn_limit: int
    turn: int = 0
    turn_results: list[TurnResult] = field(default_factory=list)
    log_entries: list[dict[str, object]] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0

    @property
    def finished(self) -> bool:
        return self.turn >= self.turn_limit

    def record(self, player: Literal["agent", "scaffold"], content: str) -> None:
        self.log_entries.append({"timestamp": datetime.now(UTC).isoformat(), "player": player, "content": content})


def _make_play_turn_tool(
    game: GameContext, secret_fn: SecretFunction, scoring_container: aiodocker.docker.DockerContainer
) -> FunctionTool:
    async def play_turn(query: int, program: str) -> str:
        """Query the secret function on one input and submit a program guess.

        Args:
            query: Integer in [0, max_input] to evaluate f on.
            program: Python program that reads a decimal integer from stdin
                     (via input()) and prints a decimal integer to stdout.
        """
        if not (0 <= query <= secret_fn.max_input):
            return json.dumps({"error": f"query must be in [0, {secret_fn.max_input}], got {query}"})

        game.turn += 1
        game.record("agent", f"Turn {game.turn}: query={query}")

        query_result = secret_fn.evaluate(query)
        logger.info("Turn %d: query=%d -> %d", game.turn, query, query_result)

        scoring = await evaluate_program(scoring_container, program, secret_fn)

        game.turn_results.append(
            TurnResult(
                turn=game.turn,
                query=query,
                query_result=query_result,
                hamming_loss=scoring.hamming_loss,
                errors=scoring.errors,
            )
        )
        game.record(
            "scaffold",
            f"Turn {game.turn}: hamming_loss={scoring.hamming_loss} "
            f"(eval {scoring.total_eval_s:.1f}s, {scoring.mean_per_input_s * 1000:.0f}ms/input)",
        )
        logger.info(
            "Turn %d: hamming_loss=%d (eval %.1fs, %.0fms/input avg, %.0fms/input max)",
            game.turn,
            scoring.hamming_loss,
            scoring.total_eval_s,
            scoring.mean_per_input_s * 1000,
            scoring.max_per_input_s * 1000,
        )

        response: dict[str, object] = {
            "turn": game.turn,
            "turns_remaining": game.turn_limit - game.turn,
            "query_result": f"f({query}) = {query_result}",
            "hamming_loss": scoring.hamming_loss,
            "total_possible_loss": (secret_fn.max_input + 1) * secret_fn.m,
        }
        if scoring.errors:
            response["errors"] = [{"input": e.input, "error": e.error} for e in scoring.errors]
            response["total_errors"] = len(scoring.errors)

        return json.dumps(response, indent=2)

    return FunctionTool(
        play_turn,
        name="play_turn",
        description=(
            "Query the secret function on one input and submit your program guess. "
            "Both query and program submission happen each turn."
        ),
    )


def _make_exec_tool(mcp_client: Client) -> FunctionTool:
    async def exec(cmd: list[str], timeout_ms: int = 30000) -> str:
        """Run a command in a scratch container for computation."""
        arguments: dict[str, object] = {"cmd": cmd, "timeout_ms": timeout_ms}
        result = await mcp_client.call_tool("exec", arguments)
        return "\n".join(block.text for block in result.content if isinstance(block, TextContent))

    return FunctionTool(
        exec, name="exec", description="Run a command in a scratch container. cmd is a list of strings (no shell)."
    )


def _build_model_client(*, api: str, model: str) -> ChatCompletionClient:
    if api == "openai":
        return OpenAIChatCompletionClient(model=model)
    if api == "anthropic":
        return CachedAnthropicClient(model=model)
    raise ValueError(f"Unsupported API: {api!r}")


async def run_game(
    *,
    variant_name: str,
    model: str,
    api: str,
    output_dir: Path,
    exec_tool: FunctionTool | None = None,
    scoring_container: aiodocker.docker.DockerContainer,
    model_client: ChatCompletionClient | None = None,
    no_skill: bool = False,
    turn_limit: int | None = None,
) -> RunSummary:
    """Execute one function learning game and persist results."""
    variant = VARIANTS[variant_name]
    secret_fn = variant.function
    effective_turn_limit = turn_limit if turn_limit is not None else variant.turn_limit

    system = build_system_prompt(skill="" if no_skill else load_skill_prompt(), has_scratch=exec_tool is not None)
    opening = first_user_message(secret_fn, effective_turn_limit, variant.function_description)

    owns_client = model_client is None
    if model_client is None:
        model_client = _build_model_client(api=api, model=model)

    game = GameContext(turn_limit=effective_turn_limit)

    # Tools: play_turn (game) + optional exec (scratch).
    play_turn_tool = _make_play_turn_tool(game, secret_fn, scoring_container)
    all_tools: list[FunctionTool] = [play_turn_tool]
    if exec_tool:
        all_tools.append(exec_tool)
    tool_schemas = [t.schema for t in all_tools]
    tool_map = {t.name: t for t in all_tools}

    history: list[SystemMessage | UserMessage | AssistantMessage | FunctionExecutionResultMessage] = [
        SystemMessage(content=system),
        UserMessage(content=opening, source="user"),
    ]

    for _ in range(_MAX_STEPS):
        if game.finished:
            break

        result = await model_client.create(history, tools=tool_schemas, tool_choice="required")

        # Track token usage (including cache metrics when available).
        game.total_input_tokens += result.usage.prompt_tokens
        game.total_output_tokens += result.usage.completion_tokens
        game.total_cache_read_tokens += getattr(result.usage, "cache_read_tokens", 0)
        game.total_cache_creation_tokens += getattr(result.usage, "cache_creation_tokens", 0)

        if isinstance(result.content, str):
            history.append(AssistantMessage(content=result.content, source="agent"))
            continue

        function_calls = result.content
        history.append(AssistantMessage(content=function_calls, source="agent"))

        exec_results: list[FunctionExecutionResult] = []
        for fc in function_calls:
            tool = tool_map.get(fc.name)
            if tool is None:
                content = f"Error: unknown tool '{fc.name}'"
                logger.warning("Unknown tool name: %s", fc.name)
                exec_results.append(FunctionExecutionResult(call_id=fc.id, content=content, name=fc.name))
                continue
            args = json.loads(fc.arguments) if isinstance(fc.arguments, str) else fc.arguments
            try:
                tool_result = await tool.run_json(args, CancellationToken())
                content = tool.return_value_as_string(tool_result)
            except Exception as e:
                content = f"Error: {e}"
                logger.warning("Tool %s error: %s", fc.name, e)
            exec_results.append(FunctionExecutionResult(call_id=fc.id, content=content, name=fc.name))
        history.append(FunctionExecutionResultMessage(content=exec_results))

    # Build result.
    per_turn = [tr.hamming_loss for tr in game.turn_results]
    total_hamming = sum(per_turn)
    fl_result = FunctionLearningResult(total_hamming_loss=total_hamming, per_turn_losses=per_turn)

    calls_path, summary_path = run_output_paths(f"fl_{variant_name}", output_dir)
    with calls_path.open("w") as f:
        for entry in game.log_entries:
            f.write(json.dumps(entry) + "\n")
        for tr in game.turn_results:
            f.write(tr.model_dump_json() + "\n")

    summary = RunSummary(
        eval_name=variant_name,
        framework="autogen",
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

    if owns_client:
        await model_client.close()
    return summary


async def _async_main(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir) if args.output_dir else Path("eval_results") / "function_learning"
    output_dir.mkdir(parents=True, exist_ok=True)

    async with scratch_exec_server() as scratch_server, Client(scratch_server) as scratch_client:
        exec_tool = _make_exec_tool(scratch_client)

        # Scoring container: plain aiodocker, no MCP.
        container_name = f"fl-scoring-{uuid.uuid4().hex[:8]}"
        async with aiodocker.Docker() as docker:
            container = await docker.containers.run(
                config={"Image": "python:3.13-slim", "Cmd": ["sleep", "3600"]}, name=container_name
            )
            try:
                summary = await run_game(
                    variant_name=args.variant,
                    model=args.model,
                    api=args.api,
                    output_dir=output_dir,
                    exec_tool=exec_tool,
                    scoring_container=container,
                    no_skill=args.no_skill,
                    turn_limit=args.turn_limit,
                )
            finally:
                await container.stop()
                await container.delete(force=True)
    logger.info("Result: %s", summary.result.model_dump_json())


def main() -> None:
    parser = argparse.ArgumentParser(description="Function Learning Eval — AutoGen")
    parser.add_argument("--variant", choices=list(VARIANTS), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api", choices=["openai", "anthropic"], default="anthropic")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--no-skill", action="store_true")
    parser.add_argument("--turn-limit", type=int, default=None, help="Override variant turn limit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
