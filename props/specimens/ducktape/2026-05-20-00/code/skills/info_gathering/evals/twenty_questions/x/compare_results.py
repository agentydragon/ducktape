"""Compare Twenty Questions results across frameworks."""

import argparse
import logging
import sys
from pathlib import Path

from skills.info_gathering.evals.twenty_questions.result_types import RunSummary

logger = logging.getLogger(__name__)


def load_summaries(results_dir: Path) -> list[RunSummary]:
    summaries: list[RunSummary] = [
        RunSummary.model_validate_json(path.read_text()) for path in sorted(results_dir.rglob("*_summary.json"))
    ]
    return summaries


def print_comparison_table(summaries: list[RunSummary]) -> None:
    if not summaries:
        print("No results found.")
        return

    header = f"{'Framework':<20} {'Eval':<15} {'Model':<20} {'API':<10} {'Turns':>5} {'Result':<15}"
    print(header)
    print("-" * len(header))

    for s in summaries:
        result_str = f"{s.result.kind}({s.turns})"
        print(f"{s.framework:<20} {s.eval_name:<15} {s.model:<20} {s.api:<10} {s.turns:>5} {result_str:<15}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Compare Twenty Questions results")
    parser.add_argument("--results-dir", required=True, help="Directory containing result files")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        print(f"Results directory not found: {results_dir}", file=sys.stderr)
        sys.exit(1)

    summaries = load_summaries(results_dir)
    print_comparison_table(summaries)


if __name__ == "__main__":
    main()
