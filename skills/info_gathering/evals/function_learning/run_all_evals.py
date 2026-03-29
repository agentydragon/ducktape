"""Run all function learning eval games: 9 functions x 2 arms (skill/no-skill)."""

import argparse
import asyncio
import logging
import uuid
from pathlib import Path

import aiodocker
import anyio
from fastmcp.client import Client
from pydantic import BaseModel

from skills.info_gathering.evals.docker_exec import scratch_exec_server
from skills.info_gathering.evals.function_learning.function_learning import make_exec_tool, run_game
from skills.info_gathering.evals.function_learning.functions import FUNCTIONS
from skills.info_gathering.evals.function_learning.result_types import FunctionLearningResult, TokenUsage

logger = logging.getLogger(__name__)

FUNCTIONS_LIST = list(FUNCTIONS.keys())


class RunRecord(BaseModel):
    function: str
    arm: str  # "skill" or "no_skill"
    run_idx: int
    model: str
    turns: int
    result: FunctionLearningResult
    usage: TokenUsage


async def run_one(
    function_name: str,
    arm_no_skill: bool,
    run_idx: int,
    exec_tool,
    scoring_container,
    sem: asyncio.Semaphore,
    model: str,
    turn_limit: int,
    output_dir: Path,
) -> RunRecord | None:
    arm = "no_skill" if arm_no_skill else "skill"
    label = f"{function_name}/{arm}[{run_idx}]"
    print(f"  START {label}", flush=True)
    async with sem:
        try:
            summary = await run_game(
                function_name=function_name,
                hint=False,
                turn_limit=turn_limit,
                model=model,
                api="anthropic",
                output_dir=output_dir,
                exec_tool=exec_tool,
                scoring_container=scoring_container,
                no_skill=arm_no_skill,
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

    async with scratch_exec_server() as scratch_server, Client(scratch_server) as scratch_client:
        exec_tool = make_exec_tool(scratch_client)

        async with aiodocker.Docker() as docker:
            container_name = f"fl-scoring-{uuid.uuid4().hex[:8]}"
            scoring_container = await docker.containers.run(
                config={"Image": "python:3.13-slim", "Cmd": ["sleep", "7200"]}, name=container_name
            )
            try:
                tasks = [
                    run_one(
                        fn_name,
                        no_skill,
                        run_idx,
                        exec_tool,
                        scoring_container,
                        sem,
                        model=args.model,
                        turn_limit=args.turn_limit,
                        output_dir=output_dir / fn_name / ("no_skill" if no_skill else "skill") / f"run_{run_idx}",
                    )
                    for fn_name in FUNCTIONS_LIST
                    for no_skill in [False, True]
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
    parser.add_argument("--turn-limit", type=int, required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--runs-per-cell", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(_async_main(args))
