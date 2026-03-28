"""Analyze function learning eval results from a runs.jsonl file.

Reads RunRecord lines, computes per-cell statistics with numpy, prints a
summary table, and optionally writes a report.md.

Usage:
    bazel run //skills/info_gathering/evals/function_learning:analyze -- \\
        --runs path/to/runs.jsonl [--report path/to/report.md]
"""

import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np

from skills.info_gathering.evals.function_learning.functions import FUNCTIONS
from skills.info_gathering.evals.function_learning.run_all_evals import RunRecord

FUNCTIONS_LIST = list(FUNCTIONS.keys())

# Pricing per million tokens: (input, output, cache_write, cache_read)
_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-4-5-20251001": (0.80, 4.00, 1.00, 0.08),
    "claude-sonnet-4-6": (3.00, 15.00, 3.75, 0.30),
    "claude-opus-4-6": (15.00, 75.00, 18.75, 1.50),
}


def load_records(path: Path) -> list[RunRecord]:
    return [RunRecord.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def print_stats(records: list[RunRecord]) -> None:
    models = sorted({r.model for r in records})
    if len(models) > 1:
        print(f"Models: {', '.join(models)}\n")
    else:
        print(f"Model: {models[0]}\n")

    cells: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for r in records:
        cells[(r.function, r.arm)].append(r)

    header = f"{'Function':<20} {'Arm':<10} {'Mean':>6} {'Std':>5} {'Min':>4} {'Max':>4} {'Solved':>7} {'Turns':>6}"
    print(header)
    print("-" * len(header))

    for fn in FUNCTIONS_LIST:
        for arm in ["skill", "no_skill"]:
            runs = cells.get((fn, arm), [])
            if not runs:
                continue
            losses = np.array([r.total_hamming_loss for r in runs])
            turns = np.array([r.turns for r in runs])
            solved = sum(1 for r in runs if r.solved_at_turn is not None)
            print(
                f"{fn:<20} {arm:<10} {losses.mean():>6.1f} {losses.std():>5.1f}"
                f" {losses.min():>4} {losses.max():>4} {solved:>3}/{len(runs):<3} {turns.mean():>6.1f}"
            )

    print()
    _print_skill_vs_noskill(cells)
    print()
    _print_cost(records)


def _print_skill_vs_noskill(cells: dict[tuple[str, str], list[RunRecord]]) -> None:
    print(f"{'Function':<20} {'Skill loss':>10} {'No-skill loss':>13} {'Winner':<10}")
    print("-" * 58)
    for fn in FUNCTIONS_LIST:
        skill_runs = cells.get((fn, "skill"), [])
        noskill_runs = cells.get((fn, "no_skill"), [])
        if not skill_runs or not noskill_runs:
            continue
        skill_mean = np.mean([r.total_hamming_loss for r in skill_runs])
        noskill_mean = np.mean([r.total_hamming_loss for r in noskill_runs])
        winner = "skill" if skill_mean < noskill_mean else "no_skill" if noskill_mean < skill_mean else "tie"
        print(f"{fn:<20} {skill_mean:>10.1f} {noskill_mean:>13.1f} {winner:<10}")


def _print_cost(records: list[RunRecord]) -> None:
    inp = sum(r.input_tokens for r in records)
    out = sum(r.output_tokens for r in records)
    cr = sum(r.cache_read_tokens for r in records)
    cw = sum(r.cache_creation_tokens for r in records)
    print(f"  tokens: inp={inp:,} out={out:,} cache_write={cw:,} cache_read={cr:,}\n")

    # Actual cost per model used in this run (grouped by model)
    by_model: dict[str, list[RunRecord]] = defaultdict(list)
    for r in records:
        by_model[r.model].append(r)

    print(f"{'Model':<30} {'Actual':>8}  (projected)")
    print("-" * 55)
    for model, (ir, or_, cwr, crr) in _PRICING.items():
        actual_records = by_model.get(model, [])
        if actual_records:
            ai = sum(r.input_tokens for r in actual_records)
            ao = sum(r.output_tokens for r in actual_records)
            acr = sum(r.cache_read_tokens for r in actual_records)
            acw = sum(r.cache_creation_tokens for r in actual_records)
            actual = ai / 1e6 * ir + ao / 1e6 * or_ + acw / 1e6 * cwr + acr / 1e6 * crr
            projected = inp / 1e6 * ir + out / 1e6 * or_ + cw / 1e6 * cwr + cr / 1e6 * crr
            print(f"{model:<30} ${actual:>7.3f}  (${projected:.3f})")
        else:
            projected = inp / 1e6 * ir + out / 1e6 * or_ + cw / 1e6 * cwr + cr / 1e6 * crr
            print(f"{model:<30} {'—':>8}  (${projected:.3f})")


def write_report(records: list[RunRecord], path: Path) -> None:
    cells: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for r in records:
        cells[(r.function, r.arm)].append(r)

    lines: list[str] = [
        "# Function Learning Benchmark Results",
        "",
        f"**Runs:** {len(records)}  ",
        "",
        "## Results by Cell",
        "",
        "| Function | Arm | Mean Loss | Std | Min | Max | Solved | Mean Turns |",
        "|----------|-----|----------:|----:|----:|----:|-------:|-----------:|",
    ]
    for fn in FUNCTIONS_LIST:
        for arm in ["skill", "no_skill"]:
            runs = cells.get((fn, arm), [])
            if not runs:
                continue
            losses = np.array([r.total_hamming_loss for r in runs])
            turns = np.array([r.turns for r in runs])
            solved = sum(1 for r in runs if r.solved_at_turn is not None)
            lines.append(
                f"| {fn} | {arm} | {losses.mean():.1f} | {losses.std():.1f}"
                f" | {losses.min()} | {losses.max()} | {solved}/{len(runs)} | {turns.mean():.1f} |"
            )

    lines += [
        "",
        "## Skill vs No-Skill",
        "",
        "| Function | Skill | No-Skill | Winner |",
        "|----------|------:|---------:|--------|",
    ]
    for fn in FUNCTIONS_LIST:
        skill = cells.get((fn, "skill"), [])
        noskill = cells.get((fn, "no_skill"), [])
        if not skill or not noskill:
            continue
        sm = np.mean([r.total_hamming_loss for r in skill])
        nm = np.mean([r.total_hamming_loss for r in noskill])
        winner = "skill" if sm < nm else "no_skill" if nm < sm else "tie"
        lines.append(f"| {fn} | {sm:.1f} | {nm:.1f} | {winner} |")

    lines += ["", "## Token Usage & Cost", ""]
    inp = sum(r.input_tokens for r in records)
    out = sum(r.output_tokens for r in records)
    cr = sum(r.cache_read_tokens for r in records)
    cw = sum(r.cache_creation_tokens for r in records)
    lines += [
        f"Input tokens: {inp:,}  ",
        f"Output tokens: {out:,}  ",
        f"Cache write tokens: {cw:,}  ",
        f"Cache read tokens: {cr:,}  ",
        "",
        "| Model | Cost |",
        "|-------|-----:|",
    ]
    for model, (ir, or_, cwr, crr) in _PRICING.items():
        cost = inp / 1e6 * ir + out / 1e6 * or_ + cw / 1e6 * cwr + cr / 1e6 * crr
        lines.append(f"| `{model}` | ${cost:.3f} |")

    path.write_text("\n".join(lines) + "\n")
    print(f"Report written to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze function learning eval results")
    parser.add_argument("--runs", type=Path, required=True, metavar="runs.jsonl")
    parser.add_argument("--report", type=Path, default=None, metavar="report.md")
    args = parser.parse_args()

    records = load_records(args.runs)
    print(f"Loaded {len(records)} run records from {args.runs}\n")
    print_stats(records)

    if args.report:
        write_report(records, args.report)


if __name__ == "__main__":
    main()
