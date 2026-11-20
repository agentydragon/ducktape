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
# Token columns of the frame, in the order of the _PRICING tuples.
_TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_write", "cache_read")


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


def _cell_stats(df: pd.DataFrame) -> pd.DataFrame:
    return (
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


def _skill_vs_noskill(df: pd.DataFrame) -> list[tuple[str, float, float, str]]:
    """Mean loss with the skill on and off, and the winner, for each function that has both arms."""
    pivot = df.pivot_table(index="function", columns="arm", values="loss", aggfunc="mean")
    rows = []
    for fn, row in pivot.iterrows():
        skill = row.get("on", float("nan"))
        noskill = row.get("off", float("nan"))
        if pd.isna(skill) or pd.isna(noskill):
            continue
        winner = "skill" if skill < noskill else "no_skill" if noskill < skill else "tie"
        rows.append((str(fn), float(skill), float(noskill), winner))
    return rows


def _token_totals(df: pd.DataFrame) -> pd.Series:
    return df[list(_TOKEN_COLUMNS)].sum()


def _cost(totals: pd.Series, prices: tuple[float, float, float, float]) -> float:
    return float(sum(totals[column] / 1e6 * price for column, price in zip(_TOKEN_COLUMNS, prices, strict=True)))


def print_stats(records: list[RunRecord]) -> None:
    df = _records_to_df(records)

    models = sorted(df["model"].unique())
    print(f"Model{'s' if len(models) > 1 else ''}: {', '.join(models)}\n")

    header = f"{'Function':<20} {'Arm':<10} {'Mean':>6} {'Std':>5} {'Min':>4} {'Max':>4} {'Solved':>7} {'Turns':>6}"
    print(header)
    print("-" * len(header))
    for _, row in _cell_stats(df).iterrows():
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
    print(f"{'Function':<20} {'Skill loss':>10} {'No-skill loss':>13} {'Winner':<10}")
    print("-" * 58)
    for fn, skill, noskill, winner in _skill_vs_noskill(df):
        print(f"{fn:<20} {skill:>10.1f} {noskill:>13.1f} {winner:<10}")


def _print_cost(df: pd.DataFrame) -> None:
    totals = _token_totals(df)
    print(
        f"  tokens: inp={totals['input_tokens']:,} out={totals['output_tokens']:,}"
        f" cache_write={totals['cache_write']:,} cache_read={totals['cache_read']:,}\n"
    )

    print(f"{'Model':<30} {'Actual':>8}  (projected)")
    print("-" * 55)
    for model, prices in _PRICING.items():
        projected = _cost(totals, prices)
        model_df = df[df["model"] == model]
        if not model_df.empty:
            print(f"{model:<30} ${_cost(_token_totals(model_df), prices):>7.3f}  (${projected:.3f})")
        else:
            print(f"{model:<30} {'—':>8}  (${projected:.3f})")


def write_report(records: list[RunRecord], path: Path) -> None:
    df = _records_to_df(records)

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
    for _, row in _cell_stats(df).iterrows():
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
    for fn, skill, noskill, winner in _skill_vs_noskill(df):
        lines.append(f"| {fn} | {skill:.1f} | {noskill:.1f} | {winner} |")

    totals = _token_totals(df)
    lines += [
        "",
        "## Token Usage & Cost",
        "",
        f"Input tokens: {totals['input_tokens']:,}  ",
        f"Output tokens: {totals['output_tokens']:,}  ",
        f"Cache write tokens: {totals['cache_write']:,}  ",
        f"Cache read tokens: {totals['cache_read']:,}  ",
        "",
        "| Model | Cost |",
        "|-------|-----:|",
    ]
    for model, prices in _PRICING.items():
        lines.append(f"| `{model}` | ${_cost(totals, prices):.3f} |")

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
