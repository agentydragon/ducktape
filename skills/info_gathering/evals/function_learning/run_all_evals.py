"""Run all function learning eval games: 9 functions x 2 arms (skill/no-skill).

Supports multiple runs per cell (--runs-per-cell N) to compute mean/std.
Generates a combined_results.json and a report.md with cost projections.
"""

import argparse
import asyncio
import json
import logging
import statistics
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiodocker
import anyio
from fastmcp.client import Client

from skills.info_gathering.evals.docker_exec import scratch_exec_server
from skills.info_gathering.evals.function_learning.function_learning import _make_exec_tool, run_game
from skills.info_gathering.evals.function_learning.functions import FUNCTIONS

logger = logging.getLogger(__name__)

FUNCTIONS_LIST = list(FUNCTIONS.keys())

# Pricing per million tokens: (input, output, cache_write, cache_read)
_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-4-5-20251001": (0.80, 4.00, 1.00, 0.08),
    "claude-sonnet-4-6": (3.00, 15.00, 3.75, 0.30),
    "claude-opus-4-6": (15.00, 75.00, 18.75, 1.50),
}


@dataclass
class RunRecord:
    function: str
    arm: str
    run_idx: int
    total_hamming_loss: int
    per_turn_losses: list[int]
    solved_at_turn: int | None
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


@dataclass
class CellStats:
    function: str
    arm: str
    runs: list[RunRecord] = field(default_factory=list)

    @property
    def losses(self) -> list[int]:
        return [r.total_hamming_loss for r in self.runs]

    @property
    def mean_loss(self) -> float:
        return statistics.mean(self.losses)

    @property
    def std_loss(self) -> float:
        return statistics.stdev(self.losses) if len(self.losses) > 1 else 0.0

    @property
    def solved_count(self) -> int:
        return sum(1 for r in self.runs if r.solved_at_turn is not None)

    @property
    def mean_turns(self) -> float:
        return statistics.mean(r.turns for r in self.runs)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.runs)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.runs)

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(r.cache_read_tokens for r in self.runs)

    @property
    def total_cache_creation_tokens(self) -> int:
        return sum(r.cache_creation_tokens for r in self.runs)


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
) -> RunRecord | dict:
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
                total_hamming_loss=summary.result.total_hamming_loss,
                per_turn_losses=summary.result.per_turn_losses,
                solved_at_turn=summary.result.solved_at_turn,
                turns=summary.turns,
                input_tokens=summary.usage.input_tokens,
                output_tokens=summary.usage.output_tokens,
                cache_read_tokens=summary.usage.cache_read_input_tokens,
                cache_creation_tokens=summary.usage.cache_creation_input_tokens,
            )
            print(
                f"  DONE  {label} loss={record.total_hamming_loss} turns={record.turns} solved={record.solved_at_turn}",
                flush=True,
            )
            return record
        except Exception as e:
            print(f"  ERROR {label}: {e}", flush=True)
            logger.exception("Error running %s", label)
            return {"function": function_name, "arm": arm, "run_idx": run_idx, "error": str(e)}


def _compute_cost(cells: list[CellStats], model: str) -> dict[str, float]:
    """Compute total cost in USD for the given cells at the given model's pricing."""
    inp_rate, out_rate, cw_rate, cr_rate = _PRICING.get(model, _PRICING["claude-haiku-4-5-20251001"])
    total_inp = sum(c.total_input_tokens for c in cells)
    total_out = sum(c.total_output_tokens for c in cells)
    total_cr = sum(c.total_cache_read_tokens for c in cells)
    total_cw = sum(c.total_cache_creation_tokens for c in cells)
    return {
        "input_tokens": total_inp,
        "output_tokens": total_out,
        "cache_read_tokens": total_cr,
        "cache_creation_tokens": total_cw,
        "cost_usd": (
            total_inp / 1e6 * inp_rate
            + total_out / 1e6 * out_rate
            + total_cr / 1e6 * cr_rate
            + total_cw / 1e6 * cw_rate
        ),
    }


def _generate_report(cells: list[CellStats], model: str, runs_per_cell: int) -> str:
    lines = [
        "# Function Learning Benchmark Results",
        "",
        f"**Model:** `{model}`  ",
        f"**Runs per cell:** {runs_per_cell}  ",
        f"**Functions:** {len(FUNCTIONS_LIST)}  ",
        "**Arms:** skill / no-skill  ",
        "",
        "## Results by Function",
        "",
        "| Function | Arm | Mean Loss | Std | Min | Max | Solved | Mean Turns |",
        "|----------|-----|----------:|----:|----:|----:|-------:|-----------:|",
    ]
    for c in cells:
        solved_rate = f"{c.solved_count}/{runs_per_cell}"
        losses = c.losses
        lines.append(
            f"| {c.function} | {c.arm} "
            f"| {c.mean_loss:.0f} | {c.std_loss:.0f} "
            f"| {min(losses)} | {max(losses)} "
            f"| {solved_rate} | {c.mean_turns:.1f} |"
        )

    lines += [
        "",
        "## Skill vs No-Skill",
        "",
        "| Function | Skill Mean Loss | No-Skill Mean Loss | Winner |",
        "|----------|----------------:|-------------------:|--------|",
    ]
    by_fn: dict[str, dict[str, CellStats]] = {}
    for c in cells:
        by_fn.setdefault(c.function, {})[c.arm] = c
    for fn in FUNCTIONS_LIST:
        arms = by_fn.get(fn, {})
        skill = arms.get("skill")
        noskill = arms.get("no_skill")
        if skill and noskill:
            winner = "skill" if skill.mean_loss < noskill.mean_loss else "no_skill"
            lines.append(f"| {fn} | {skill.mean_loss:.0f} | {noskill.mean_loss:.0f} | {winner} |")

    lines += ["", "## Token Usage & Cost", ""]

    actual_cost = _compute_cost(cells, model)
    lines += [
        f"Actual run ({model}):",
        "",
        "| Metric | Value |",
        "|--------|------:|",
        f"| Input tokens | {actual_cost['input_tokens']:,} |",
        f"| Output tokens | {actual_cost['output_tokens']:,} |",
        f"| Cache creation tokens | {actual_cost['cache_creation_tokens']:,} |",
        f"| Cache read tokens | {actual_cost['cache_read_tokens']:,} |",
        f"| **Total cost** | **${actual_cost['cost_usd']:.3f}** |",
        "",
    ]

    # Project costs for other models using same token mix
    total_inp = actual_cost["input_tokens"]
    total_out = actual_cost["output_tokens"]
    total_cr = actual_cost["cache_read_tokens"]
    total_cw = actual_cost["cache_creation_tokens"]

    lines += [
        "Cost projections (same token mix, different models):",
        "",
        "| Model | Input $/M | Output $/M | Cache Write $/M | Cache Read $/M | Projected Cost |",
        "|-------|----------:|-----------:|----------------:|---------------:|---------------:|",
    ]
    for proj_model, (ir, or_, cwr, crr) in _PRICING.items():
        cost = total_inp / 1e6 * ir + total_out / 1e6 * or_ + total_cw / 1e6 * cwr + total_cr / 1e6 * crr
        lines.append(f"| `{proj_model}` | ${ir} | ${or_} | ${cwr} | ${crr} | **${cost:.2f}** |")

    lines += ["", "*(Projections assume same number of tokens; actual costs may differ by model.)*", ""]
    return "\n".join(lines)


async def _async_main(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    await anyio.Path(output_dir).mkdir(parents=True, exist_ok=True)

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
                await scoring_container.stop()
                await scoring_container.delete(force=True)

    # Group into cells
    cell_map: dict[tuple[str, str], CellStats] = {}
    errors = []
    for r in raw_results:
        if isinstance(r, RunRecord):
            key = (r.function, r.arm)
            if key not in cell_map:
                cell_map[key] = CellStats(function=r.function, arm=r.arm)
            cell_map[key].runs.append(r)
        else:
            errors.append(r)

    cells = list(cell_map.values())

    # Serialize
    combined: list[dict] = []
    for c in cells:
        entry: dict = {
            "function": c.function,
            "arm": c.arm,
            "n_runs": len(c.runs),
            "mean_loss": c.mean_loss,
            "std_loss": c.std_loss,
            "min_loss": min(c.losses),
            "max_loss": max(c.losses),
            "solved_count": c.solved_count,
            "mean_turns": c.mean_turns,
            "total_input_tokens": c.total_input_tokens,
            "total_output_tokens": c.total_output_tokens,
            "total_cache_read_tokens": c.total_cache_read_tokens,
            "total_cache_creation_tokens": c.total_cache_creation_tokens,
            "runs": [
                {
                    "run_idx": r.run_idx,
                    "total_hamming_loss": r.total_hamming_loss,
                    "per_turn_losses": r.per_turn_losses,
                    "solved_at_turn": r.solved_at_turn,
                    "turns": r.turns,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cache_read_tokens": r.cache_read_tokens,
                    "cache_creation_tokens": r.cache_creation_tokens,
                }
                for r in c.runs
            ],
        }
        combined.append(entry)
    if errors:
        combined.append({"errors": errors})

    combined_path = output_dir / "combined_results.json"
    combined_path.write_text(json.dumps(combined, indent=2))

    report = _generate_report(cells, args.model, args.runs_per_cell)
    report_path = output_dir / "report.md"
    report_path.write_text(report)

    print("\nAll done.")
    print(f"  Results: {combined_path}")
    print(f"  Report:  {report_path}")
    if errors:
        print(f"  Errors:  {len(errors)} run(s) failed — see combined_results.json")


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
