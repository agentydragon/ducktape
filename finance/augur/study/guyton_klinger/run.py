"""Drive composed annual windows through the Guyton-Klinger policy, and the offline CLI.

Every window opens at the adaptation targets. The CLI runs a fresh <policy.py> `Policy` for
the population and another for the selected trace replay; `records.json` carries each path's
per-year intentions, and paid amounts are the receipts' in `outcomes.json`.

    bbr run //finance/augur/study/guyton_klinger:run_bin -- --synthetic --years 30 \\
      --initial-wealth 1000000 --initial-rate 0.05 --output-dir /tmp/gk --trace-rollout 2
"""

import argparse
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel, Field

from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import Capture
from finance.augur.study.guyton_klinger.panel import Sleeve, load_panel
from finance.augur.study.guyton_klinger.paths import (
    ADAPTATION_TARGET_PERCENT,
    RETIREE,
    AnnualWindows,
    annual_windows,
    compose_world,
    eligible_start_years,
)
from finance.augur.study.guyton_klinger.policy import Cell, Guardrail, Inflation, Policy, Stage
from finance.augur.study.guyton_klinger.synthetic import synthetic_panel


def run(
    windows: AnnualWindows,
    cell: Cell,
    *,
    wealth: Decimal,
    weights: Mapping[Sleeve, int],
    rollout_ids: Sequence[int] | None = None,
    capture: Capture = "summary",
) -> tuple[list[Rollout], Policy]:
    """Selected original rollout IDs, in the given order, under a fresh `Policy` for `cell`.

    Selected replay reuses `windows`, never rematerializes them; the returned policy holds
    each path's `YearRecord`s.
    """
    ids = range(len(windows.start_years)) if rollout_ids is None else rollout_ids
    if len(set(ids)) != len(ids):
        raise ValueError(f"{rollout_ids=} must be distinct")
    policy = Policy(cell)
    session = ActionSession(
        {id_: compose_world(windows, id_, wealth=wealth, weights=weights) for id_ in ids}, RETIREE, capture=capture
    )
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(policy(batch))
        return batch.rollouts, policy
    finally:
        session.close()


class YearRecordView(BaseModel):
    """One review's `policy.YearRecord` as the CLI writes it; money in currency quanta."""

    year: int = Field(description="Zero-based year index within the window")
    opening_wealth: int
    withdrawal: int = Field(description="Requested amount, half up; the paid amount is in the payment receipts")
    inflation: Inflation
    guardrail: Guardrail
    funding: dict[Stage, int] = Field(description="Proceeds per funding stage, in funding order")


class PathRecords(BaseModel):
    rollout_id: int
    start_year: int
    years: list[YearRecordView]


class Records(BaseModel):
    paths: list[PathRecords]


def path_records(windows: AnnualWindows, policy: Policy, rollouts: Sequence[Rollout]) -> Records:
    return Records(
        paths=[
            PathRecords(
                rollout_id=rollout.rollout_id,
                start_year=windows.start_years[rollout.rollout_id],
                years=[
                    YearRecordView(
                        year=record.year,
                        opening_wealth=record.opening_wealth,
                        withdrawal=record.requested,
                        inflation=record.spending.inflation,
                        guardrail=record.spending.guardrail,
                        funding=dict(record.funding),
                    )
                    for record in policy.memory[rollout.rollout_id].records
                ],
            )
            for rollout in rollouts
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Annual three-sleeve replay of the Guyton-Klinger 2006 rules")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--panel", type=Path, help="Annual panel CSV; see panel.py for the format")
    source.add_argument("--synthetic", action="store_true", help="Generated placeholder panel, not evidence")
    parser.add_argument("--years", type=int, required=True)
    parser.add_argument("--start-year", type=int, action="append", help="Default: every complete window")
    parser.add_argument("--initial-wealth", type=Decimal, required=True)
    parser.add_argument("--initial-rate", type=Fraction, required=True, help="Year-0 withdrawal over wealth, e.g. 0.05")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--trace-rollout", type=int, action="append", default=[])
    args = parser.parse_args()
    panel = synthetic_panel(args.years) if args.synthetic else load_panel(args.panel)
    start_years = args.start_year or eligible_start_years(panel, args.years)
    windows = annual_windows(panel, start_years=start_years, years=args.years)
    cell = Cell(initial_rate=args.initial_rate, years=args.years)

    def replay(rollout_ids: Sequence[int] | None, capture: Capture) -> tuple[list[Rollout], Policy]:
        return run(
            windows,
            cell,
            wealth=args.initial_wealth,
            weights=ADAPTATION_TARGET_PERCENT,
            rollout_ids=rollout_ids,
            capture=capture,
        )

    outcomes, policy = replay(None, "summary")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "outcomes.json").write_text(Finished(rollouts=outcomes).model_dump_json())
    (args.output_dir / "records.json").write_text(path_records(windows, policy, outcomes).model_dump_json())
    (args.output_dir / "study.json").write_text(
        json.dumps(
            {
                "source": "synthetic placeholder panel" if args.synthetic else str(args.panel),
                "policy": "Guyton-Klinger 2006 rules, declared three-sleeve adaptation",
                "start_years": windows.start_years,
                "years": windows.years,
                "target_percent": ADAPTATION_TARGET_PERCENT,
                "initial_wealth": str(args.initial_wealth),
                "initial_rate": str(args.initial_rate),
                "completed_windows": sum(row.stop is None for row in outcomes),
            }
        )
    )
    if args.trace_rollout:
        traces, _ = replay(args.trace_rollout, "forensic")
        (args.output_dir / "traces.json").write_text(Finished(rollouts=traces).model_dump_json())
    print(f"Saved {len(outcomes)} overlapping windows to {args.output_dir}; not independent trials.")


if __name__ == "__main__":
    main()
