"""Bench script for the spike-1 representative scenario.

Runs `build_bench_scenario()` end-to-end at 1000 rollouts and
reports wall-clock time. No specific perf target this round —
the goal is to establish the baseline that later waves' bench
targets are set against. Invoke via `bb run`.
"""

from __future__ import annotations

import argparse
import time

from augur.sim.bench_scenario import build_bench_scenario
from augur.sim.simulate import simulate


def main() -> None:
    parser = argparse.ArgumentParser(description="Spike-1 augur sim bench")
    parser.add_argument("--rollouts", type=int, default=1000)
    parser.add_argument("--horizon-months", type=int, default=60)
    args = parser.parse_args()

    scenario = build_bench_scenario(horizon_months=args.horizon_months)

    t0 = time.perf_counter()
    result = simulate(scenario, rollout_count=args.rollouts)
    elapsed = time.perf_counter() - t0

    print(f"rollouts: {args.rollouts}")
    print(f"horizon_months: {args.horizon_months}")
    print(f"wall_clock_sec: {elapsed:.3f}")
    print(f"cash_balances_rows: {result.cash_balances.height}")
    print(f"asset_lots_rows: {result.asset_lots.height}")
    print(f"market_prices_rows: {result.market_prices.height}")
    print(f"transfers: {result.events_log.transfers.height}")
    print(f"lot_dispositions: {result.events_log.lot_dispositions.height}")
    print(f"tax_accruals: {result.events_log.tax_accruals.height}")
    print(f"rollout_failures: {result.events_log.rollout_failures.height}")


if __name__ == "__main__":
    main()
