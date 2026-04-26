"""Run all function learning eval games: 9 functions x 2 arms (skill on/off)."""

import argparse
import asyncio
import logging
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

import aiodocker
import anyio
from agent_framework import MCPStdioTool
from pydantic import BaseModel

from skills.eval_infra.af_chat_client import build_model_client
from skills.eval_infra.eval_sandbox import eval_sandbox
from skills.eval_infra.skill_staging import stage_skill
from skills.info_gathering.evals.function_learning.function_learning import _MAX_STEPS, SKILL_BY_ARM, run_game
from skills.info_gathering.evals.function_learning.functions import FUNCTIONS
from skills.info_gathering.evals.function_learning.result_types import FunctionLearningResult, TokenUsage

logger = logging.getLogger(__name__)

FUNCTIONS_LIST = list(FUNCTIONS.keys())


class RunRecord(BaseModel):
    function: str
    arm: str  # "on" or "off"
    run_idx: int
    model: str
    turns: int
    result: FunctionLearningResult
    usage: TokenUsage


async def run_one(
    function_name: str,
    arm: str,
    run_idx: int,
    exec_tool: MCPStdioTool,
    skill_md: str,
    scoring_container,
    model_client,
    sem: asyncio.Semaphore,
    model: str,
    api: str,
    turn_limit: int,
    output_dir: Path,
) -> RunRecord | None:
    label = f"{function_name}/{arm}[{run_idx}]"
    print(f"  START {label}", flush=True)
    async with sem:
        try:
            summary = await run_game(
                function_name=function_name,
                hint=False,
                turn_limit=turn_limit,
                model=model,
                api=api,
                output_dir=output_dir,
                exec_tool=exec_tool,
                scoring_container=scoring_container,
                model_client=model_client,
                skill_md=skill_md,
            )
            record = RunRecord(
                function=function_name,
                arm=arm,
                run_idx=run_idx,
                model=summary.model,
                turns=summary.turns,
                result=summary.result,
                usage=summary.usage,
            )
            print(
                f"  DONE  {label} loss={record.result.total_hamming_loss} turns={record.turns} solved={record.result.solved_at_turn}",
                flush=True,
            )
            return record
        except Exception as e:
            print(f"  ERROR {label}: {e}", flush=True)
            logger.exception("Error running %s", label)
            return None


async def _async_main(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    await anyio.Path(output_dir).mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(args.concurrency)
    model_client = build_model_client(
        api=args.api, model=args.model, function_invocation_configuration={"max_iterations": _MAX_STEPS}
    )

    # Extract each arm's skill tar once and spin up one exec_tool per arm.
    arms: dict[str, tuple[MCPStdioTool, str]] = {}
    async with AsyncExitStack() as stack:
        for arm in ("on", "off"):
            staged = stage_skill(SKILL_BY_ARM[arm], output_dir / f"skill_extract_{arm}")
            exec_tool = await stack.enter_async_context(
                eval_sandbox(skill=staged, workspace=output_dir / f"work_{arm}", inputs=None)
            )
            arms[arm] = (exec_tool, staged.md_text)

        docker = await stack.enter_async_context(aiodocker.Docker())
        container_name = f"fl-scoring-{uuid.uuid4().hex[:8]}"
        scoring_container = await docker.containers.run(
            config={"Image": "python:3.13-slim", "Cmd": ["sleep", "7200"]}, name=container_name
        )
        try:
            tasks = [
                run_one(
                    fn_name,
                    arm,
                    run_idx,
                    arms[arm][0],
                    arms[arm][1],
                    scoring_container,
                    model_client,
                    sem,
                    model=args.model,
                    api=args.api,
                    turn_limit=args.turn_limit,
                    output_dir=output_dir / fn_name / arm / f"run_{run_idx}",
                )
                for fn_name in FUNCTIONS_LIST
                for arm in ("on", "off")
                for run_idx in range(args.runs_per_cell)
            ]
            raw_results = await asyncio.gather(*tasks)
        finally:
            await scoring_container.delete(force=True)

    records = [r for r in raw_results if r is not None]
    error_count = len(raw_results) - len(records)

    runs_path = output_dir / "runs.jsonl"
    with runs_path.open("w") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")

    print(f"\nAll done: {len(records)} runs saved to {runs_path}")
    if error_count:
        print(f"  {error_count} run(s) failed (see stderr above)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Run all function learning evals")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--api", choices=["openai", "anthropic"], default="anthropic")
    parser.add_argument("--turn-limit", type=int, required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--runs-per-cell", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(_async_main(args))
