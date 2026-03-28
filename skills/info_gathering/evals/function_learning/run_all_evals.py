"""Run all function learning eval games: 9 functions x 2 arms (skill/no-skill)."""

import argparse
import asyncio
import json
import logging
import uuid
from pathlib import Path

import aiodocker
from fastmcp.client import Client

from skills.info_gathering.evals.docker_exec import scratch_exec_server
from skills.info_gathering.evals.function_learning.function_learning import _make_exec_tool, run_game
from skills.info_gathering.evals.function_learning.functions import FUNCTIONS

logger = logging.getLogger(__name__)

FUNCTIONS_LIST = list(FUNCTIONS.keys())


async def run_one(
    function_name: str,
    no_skill: bool,
    exec_tool,
    scoring_container,
    sem: asyncio.Semaphore,
    model: str,
    turn_limit: int,
    output_dir: Path,
) -> dict:
    arm = "no_skill" if no_skill else "skill"
    print(f"  START {function_name}/{arm}", flush=True)
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
                no_skill=no_skill,
            )
            print(
                f"  DONE  {function_name}/{arm} "
                f"loss={summary.result.total_hamming_loss} "
                f"turns={summary.turns} "
                f"solved={summary.result.solved_at_turn}",
                flush=True,
            )
            return {"function": function_name, "arm": arm, "summary": summary}
        except Exception as e:
            print(f"  ERROR {function_name}/{arm}: {e}", flush=True)
            logger.exception("Error running %s/%s", function_name, arm)
            return {"function": function_name, "arm": arm, "error": str(e)}


async def _async_main(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    Path.mkdir(output_dir, parents=True, exist_ok=True)

    sem = asyncio.Semaphore(args.concurrency)

    async with scratch_exec_server() as scratch_server, Client(scratch_server) as scratch_client:
        exec_tool = _make_exec_tool(scratch_client)

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
                        exec_tool,
                        scoring_container,
                        sem,
                        model=args.model,
                        turn_limit=args.turn_limit,
                        output_dir=output_dir,
                    )
                    for fn_name in FUNCTIONS_LIST
                    for no_skill in [False, True]
                ]

                results = await asyncio.gather(*tasks)
            finally:
                await scoring_container.stop()
                await scoring_container.delete(force=True)

    out = []
    for r in results:
        if "summary" in r:
            s = r["summary"]
            out.append(
                {
                    "function": r["function"],
                    "arm": r["arm"],
                    "total_hamming_loss": s.result.total_hamming_loss,
                    "per_turn_losses": s.result.per_turn_losses,
                    "solved_at_turn": s.result.solved_at_turn,
                    "turns": s.turns,
                    "input_tokens": s.usage.input_tokens,
                    "output_tokens": s.usage.output_tokens,
                    "cache_read_tokens": s.usage.cache_read_input_tokens,
                    "cache_creation_tokens": s.usage.cache_creation_input_tokens,
                }
            )
        else:
            out.append(r)

    combined = output_dir / "combined_results.json"
    combined.write_text(json.dumps(out, indent=2))
    print("\nAll done. Results written to", combined)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Run all function learning evals")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--turn-limit", type=int, required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(_async_main(args))
