"""Analyze function learning eval results from a runs.jsonl file.

Reads RunRecord lines, computes per-cell statistics, prints a summary table,
and optionally writes a report.md.

Usage:
    bazel run //skills/info_gathering/evals/function_learning:analyze -- \\
        --runs path/to/runs.jsonl [--report path/to/report.md]
"""

import argparse
from pathlib import Path

import pandas as pd

from skills.info_gathering.evals.function_learning.run_all_evals import RunRecord

# Pricing per million tokens: (input, output, cache_write, cache_read)
_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-4-5-20251001": (0.80, 4.00, 1.00, 0.08),
    "claude-sonnet-4-6": (3.00, 15.00, 3.75, 0.30),
    "claude-opus-4-6": (15.00, 75.00, 18.75, 1.50),
}


def load_records(path: Path) -> list[RunRecord]:
    return [RunRecord.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def _records_to_df(records: list[RunRecord]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "function": r.function,
            "arm": r.arm,
            "model": r.model,
            "loss": r.result.total_hamming_loss,
            "turns": r.turns,
            "solved": r.result.solved_at_turn is not None,
            "input_tokens": r.usage.input_tokens,
            "output_tokens": r.usage.output_tokens,
            "cache_read": r.usage.cache_read_input_tokens,
            "cache_write": r.usage.cache_creation_input_tokens,
        }
        for r in records
    )


def print_stats(records: list[RunRecord]) -> None:
    df = _records_to_df(records)

    models = sorted(df["model"].unique())
    print(f"Model{'s' if len(models) > 1 else ''}: {', '.join(models)}\n")

    stats = (
        df.groupby(["function", "arm"])
        .agg(
            mean_loss=("loss", "mean"),
            std_loss=("loss", "std"),
            min_loss=("loss", "min"),
            max_loss=("loss", "max"),
            solved=("solved", "sum"),
            n=("loss", "count"),
            mean_turns=("turns", "mean"),
        )
        .reset_index()
    )

    header = f"{'Function':<20} {'Arm':<10} {'Mean':>6} {'Std':>5} {'Min':>4} {'Max':>4} {'Solved':>7} {'Turns':>6}"
    print(header)
    print("-" * len(header))
    for _, row in stats.iterrows():
        print(
            f"{row['function']:<20} {row['arm']:<10} {row['mean_loss']:>6.1f} {row['std_loss']:>5.1f}"
            f" {int(row['min_loss']):>4} {int(row['max_loss']):>4}"
            f" {int(row['solved']):>3}/{int(row['n']):<3} {row['mean_turns']:>6.1f}"
        )

    print()
    _print_skill_vs_noskill(df)
    print()
    _print_cost(df)


def _print_skill_vs_noskill(df: pd.DataFrame) -> None:
    pivot = df.pivot_table(index="function", columns="arm", values="loss", aggfunc="mean")
    print(f"{'Function':<20} {'Skill loss':>10} {'No-skill loss':>13} {'Winner':<10}")
    print("-" * 58)
    for fn, row in pivot.iterrows():
        skill = row.get("skill", float("nan"))
        noskill = row.get("no_skill", float("nan"))
        if pd.isna(skill) or pd.isna(noskill):
            continue
        winner = "skill" if skill < noskill else "no_skill" if noskill < skill else "tie"
        print(f"{fn:<20} {skill:>10.1f} {noskill:>13.1f} {winner:<10}")


def _print_cost(df: pd.DataFrame) -> None:
    inp = df["input_tokens"].sum()
    out = df["output_tokens"].sum()
    cr = df["cache_read"].sum()
    cw = df["cache_write"].sum()
    print(f"  tokens: inp={inp:,} out={out:,} cache_write={cw:,} cache_read={cr:,}\n")

    print(f"{'Model':<30} {'Actual':>8}  (projected)")
    print("-" * 55)
    for model, (ir, or_, cwr, crr) in _PRICING.items():
        projected = inp / 1e6 * ir + out / 1e6 * or_ + cw / 1e6 * cwr + cr / 1e6 * crr
        model_df = df[df["model"] == model]
        if not model_df.empty:
            ai, ao, acr, acw = (
                model_df["input_tokens"].sum(),
                model_df["output_tokens"].sum(),
                model_df["cache_read"].sum(),
                model_df["cache_write"].sum(),
            )
            actual = ai / 1e6 * ir + ao / 1e6 * or_ + acw / 1e6 * cwr + acr / 1e6 * crr
            print(f"{model:<30} ${actual:>7.3f}  (${projected:.3f})")
        else:
            print(f"{model:<30} {'—':>8}  (${projected:.3f})")


def write_report(records: list[RunRecord], path: Path) -> None:
    df = _records_to_df(records)

    stats = (
        df.groupby(["function", "arm"])
        .agg(
            mean_loss=("loss", "mean"),
            std_loss=("loss", "std"),
            min_loss=("loss", "min"),
            max_loss=("loss", "max"),
            solved=("solved", "sum"),
            n=("loss", "count"),
            mean_turns=("turns", "mean"),
        )
        .reset_index()
    )

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
    for _, row in stats.iterrows():
        lines.append(
            f"| {row['function']} | {row['arm']} | {row['mean_loss']:.1f} | {row['std_loss']:.1f}"
            f" | {int(row['min_loss'])} | {int(row['max_loss'])} | {int(row['solved'])}/{int(row['n'])} | {row['mean_turns']:.1f} |"
        )

    lines += [
        "",
        "## Skill vs No-Skill",
        "",
        "| Function | Skill | No-Skill | Winner |",
        "|----------|------:|---------:|--------|",
    ]
    pivot = df.pivot_table(index="function", columns="arm", values="loss", aggfunc="mean")
    for fn, row in pivot.iterrows():
        skill = row.get("skill", float("nan"))
        noskill = row.get("no_skill", float("nan"))
        if pd.isna(skill) or pd.isna(noskill):
            continue
        winner = "skill" if skill < noskill else "no_skill" if noskill < skill else "tie"
        lines.append(f"| {fn} | {skill:.1f} | {noskill:.1f} | {winner} |")

    inp = df["input_tokens"].sum()
    out = df["output_tokens"].sum()
    cr = df["cache_read"].sum()
    cw = df["cache_write"].sum()
    lines += [
        "",
        "## Token Usage & Cost",
        "",
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
