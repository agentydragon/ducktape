"""Drive composed annual windows through a caller's batch policy, and the offline CLI.

The CLI runs a fixed nominal annual withdrawal: a plumbing placeholder, not the
Guyton-Klinger rules. Its withdrawals are overweight-first/FIFO sales with no
reinvestment or rebalancing.

    bbr run //finance/augur/study/guyton_klinger:run_bin -- --synthetic --years 30 \\
      --initial-wealth 1000000 --withdrawal 40000 --output-dir /tmp/gk --trace-rollout 2
"""

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path

from finance.augur.policy.sleeves import withdraw
from finance.augur.sim.actions import Action, Consume, DecisionActions
from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import currency_amount_to_quanta
from finance.augur.sim.ids import AccountId, AssetId
from finance.augur.sim.observations import Decision
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import Capture
from finance.augur.study.guyton_klinger.panel import Sleeve, load_panel
from finance.augur.study.guyton_klinger.paths import (
    ADAPTATION_TARGET_PERCENT,
    CHECKING,
    MONTHS_PER_YEAR,
    QUANTUM,
    RETIREE,
    WORLD,
    AnnualWindows,
    annual_windows,
    compose_world,
    eligible_start_years,
    sleeve_targets,
)
from finance.augur.study.guyton_klinger.synthetic import synthetic_panel

type BatchPolicy = Callable[[list[Decision]], list[DecisionActions]]


def run(
    windows: AnnualWindows,
    policy: BatchPolicy,
    *,
    wealth: Decimal,
    weights: Mapping[Sleeve, int],
    rollout_ids: Sequence[int] | None = None,
    capture: Capture = "summary",
) -> list[Rollout]:
    """Selected original rollout IDs, in the given order, under one batch policy.

    `policy` answers every active window each month and owns any per-window memory, so
    pass a fresh one per run; selected replay reuses `windows`, never rematerializes them.
    """
    ids = range(len(windows.start_years)) if rollout_ids is None else rollout_ids
    if len(set(ids)) != len(ids):
        raise ValueError(f"{rollout_ids=} must be distinct")
    session = ActionSession(
        {id_: compose_world(windows, id_, wealth=wealth, weights=weights) for id_ in ids}, RETIREE, capture=capture
    )
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(policy(batch))
        return batch.rollouts
    finally:
        session.close()


def fixed_nominal_withdrawal(amount: int, *, targets: dict[tuple[AccountId, AssetId], int]) -> BatchPolicy:
    """Consume `amount` quanta each January, selling whatever checking cannot cover.

    Nothing is consumed at the terminal mark, which no decision precedes. An unfunded
    withdrawal is rejected and stops that window, keeping its sales.
    """

    def policy(batch: list[Decision]) -> list[DecisionActions]:
        responses = []
        for decision in batch:
            observation = decision.observation
            actions: list[Action] = []
            if observation.month % MONTHS_PER_YEAR == 0:
                actions = withdraw(
                    observation,
                    targets=targets,
                    cash_account_id=CHECKING,
                    amount=max(0, amount - dict(observation.accounts)[CHECKING]),
                    cause_id=f"withdrawal-funding-m{observation.month}",
                )
                actions.append(
                    Consume(
                        request_id=0,
                        cause_id=f"withdrawal-m{observation.month}",
                        component_id="annual_withdrawal",
                        from_account=AccountRef(agent_id=RETIREE, account_id=CHECKING),
                        to_account=AccountRef(agent_id=WORLD, account_id=CHECKING),
                        amount=amount,
                    )
                )
            responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
        return responses

    return policy


def main() -> None:
    parser = argparse.ArgumentParser(description="Annual three-sleeve replay under a fixed nominal withdrawal")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--panel", type=Path, help="Annual panel CSV; see panel.py for the format")
    source.add_argument("--synthetic", action="store_true", help="Generated placeholder panel, not evidence")
    parser.add_argument("--years", type=int, required=True)
    parser.add_argument("--start-year", type=int, action="append", help="Default: every complete window")
    parser.add_argument("--initial-wealth", type=Decimal, required=True)
    parser.add_argument("--withdrawal", type=Decimal, required=True, help="Nominal amount each January")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--trace-rollout", type=int, action="append", default=[])
    args = parser.parse_args()
    panel = synthetic_panel(args.years) if args.synthetic else load_panel(args.panel)
    start_years = args.start_year or eligible_start_years(panel, args.years)
    windows = annual_windows(panel, start_years=start_years, years=args.years)
    targets = sleeve_targets(ADAPTATION_TARGET_PERCENT)
    amount = int(currency_amount_to_quanta(args.withdrawal, quantum=QUANTUM))

    def cell(rollout_ids: Sequence[int] | None, capture: Capture) -> list[Rollout]:
        return run(
            windows,
            fixed_nominal_withdrawal(amount, targets=targets),
            wealth=args.initial_wealth,
            weights=ADAPTATION_TARGET_PERCENT,
            rollout_ids=rollout_ids,
            capture=capture,
        )

    outcomes = cell(None, "summary")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "outcomes.json").write_text(Finished(rollouts=outcomes).model_dump_json())
    (args.output_dir / "study.json").write_text(
        json.dumps(
            {
                "source": "synthetic placeholder panel" if args.synthetic else str(args.panel),
                "policy": "fixed nominal annual withdrawal placeholder, not the Guyton-Klinger rules",
                "start_years": windows.start_years,
                "years": windows.years,
                "target_percent": ADAPTATION_TARGET_PERCENT,
                "initial_wealth": str(args.initial_wealth),
                "withdrawal": str(args.withdrawal),
                "completed_windows": sum(row.stop is None for row in outcomes),
            }
        )
    )
    if args.trace_rollout:
        traces = cell(args.trace_rollout, "forensic")
        (args.output_dir / "traces.json").write_text(Finished(rollouts=traces).model_dump_json())
    print(f"Saved {len(outcomes)} overlapping windows to {args.output_dir}; not independent trials.")


if __name__ == "__main__":
    main()
