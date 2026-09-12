"""Constant versus annual glide targets with canonical funding and holdings.

Three stipulated price/CPI paths, not sampled forecasts or historical evidence.
The two test securities have no payouts; tax, fees, housing and borrowing are absent.
This is a composition example, not a named retirement-study reproduction.
"""

import argparse
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import numpy as np

from finance.augur.model.series import InflationKey, SecurityKey
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.ids import AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedIndexedAmount,
    PreparedLot,
    PreparedObligation,
    PreparedSeries,
)
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World
from finance.augur.x.allocation_glide.policy import decide

QUANTUM = Decimal("0.01")
RETIREE = AgentId("test-retiree")
COUNTERPARTY = "test-world"
GROWTH = SecurityKey(symbol="test-growth")
STEADY = SecurityKey(symbol="test-steady")
HORIZON_MONTHS = 60


@dataclass(frozen=True)
class Situation:
    """What every path shares: the three stipulated paths and the horizon; the books are declared per path."""

    series: tuple[PreparedSeries, ...]
    rollout_count: int


def situation() -> Situation:
    month = np.arange(HORIZON_MONTHS + 1)
    growth = np.full((3, HORIZON_MONTHS + 1), 100.0)
    growth[0, 12:] = 80.0
    growth[1, 12:] = 120.0
    steady = np.full_like(growth, 100.0)
    cpi = np.broadcast_to(1.025 ** (month // 12), growth.shape)
    paths = ExternalSeriesContext.from_level_blocks(
        [(GROWTH, growth), (STEADY, steady), (InflationKey(), cpi)], rollout_count=3, horizon_months=HORIZON_MONTHS
    )
    return Situation(
        series=compile_series(paths, rollout_count=3, horizon_months=HORIZON_MONTHS, currency_quantum=QUANTUM),
        rollout_count=3,
    )


def compose(case: Situation, rollout_id: int) -> World:
    """USD 10,000 cash, 500 units of each security at USD 100 basis, and a CPI-indexed USD 6,000 yearly spend."""
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=HORIZON_MONTHS,
        income_sources=(ORDINARY_INCOME,),
    )
    for name, balance in ((RETIREE, Decimal(10_000)), (COUNTERPARTY, Decimal(0))):
        world.declare_account(
            PreparedAccount(
                account=AccountRef(agent_id=name, account_id="checking"),
                opening_balance=int(currency_amount_to_quanta(balance, quantum=QUANTUM)),
            )
        )
    for asset in (GROWTH, STEADY):
        scale = quantity_scale_for_asset(asset)
        world.declare_pool(
            PreparedHoldingPool(
                agent_id=RETIREE, account_id="checking", asset_id=str(asset.symbol), quantity_scale=scale
            )
        )
        world.hold(
            PreparedLot(
                lot_id=f"test-opening-{asset.symbol}",
                agent_id=RETIREE,
                account_id="checking",
                asset_id=str(asset.symbol),
                purchase_month=-24,
                quantity_scale=scale,
                units=int(quantity_to_quanta(500, scale=scale)),
                basis=int(currency_amount_to_quanta(Decimal(50_000), quantum=QUANTUM)),
            )
        )
    for month in range(0, HORIZON_MONTHS, 12):
        world.track(
            Biller(
                PreparedObligation(
                    month=month,
                    obligation_id="test-consumption",
                    obligation_type="cash_spend",
                    from_account=AccountRef(agent_id=RETIREE, account_id="checking"),
                    to_account=AccountRef(agent_id=COUNTERPARTY, account_id="checking"),
                    amount_due=PreparedIndexedAmount(
                        base_amount=int(currency_amount_to_quanta(Decimal(6_000), quantum=QUANTUM)),
                        series_id=InflationKey().wire_id,
                        base_month_index=0,
                        adjustment_period_months=12,
                    ),
                    property_id=None,
                    deduction_category=None,
                    deductible_fraction_ppb=1_000_000_000,
                )
            )
        )
    return world


def execute(
    case: Situation,
    *,
    annual_step: int,
    rollout_ids: list[int],
    capture: Literal["summary", "dense", "forensic"] = "summary",
) -> list[Rollout]:
    session = ActionSession({id_: compose(case, id_) for id_ in rollout_ids}, RETIREE, capture=capture)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(decide(batch, annual_step=annual_step))
        return batch.rollouts
    finally:
        session.close()


def compare(output_dir: Path) -> None:
    """Compose the same situation for every cell; retain compact populations and independently replay selected traces."""
    run = situation()
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, step in (("constant", 0), ("glide", 5)):
        population = execute(run, annual_step=step, rollout_ids=[0, 1, 2])
        (output_dir / f"{name}.json").write_text(Finished(rollouts=population).model_dump_json())
        traces = execute(run, annual_step=step, rollout_ids=[2, 0], capture="forensic")
        (output_dir / f"{name}-traces.json").write_text(Finished(rollouts=traces).model_dump_json())
        for rollout in population:
            paid = sum(
                row.receipt.amount_paid
                for row in rollout.summary.payments
                if row.target is not None and row.target.obligation_type == "cash_spend"
            )
            print(f"{name} path={rollout.rollout_id}: consumption_paid=${paid / 100:.2f}; stop={rollout.stop}")
    (output_dir / "experiment.json").write_text(
        json.dumps(
            {
                "paths": "three stipulated paths: growth moves to 80/120/100 at month 12; steady stays 100; CPI rises 2.5% annually",
                "taxes": False,
                "payouts": False,
                "transaction_costs": False,
                "starting_cash_usd": 10_000,
                "opening_sleeve_value_usd": 50_000,
                "annual_real_consumption_usd": 6_000,
                "horizon_months": HORIZON_MONTHS,
                "cash_band_usd": [0, 10_000],
                "allow_purchases": True,
                "quiet_band_drift_tolerance": 0,
                "growth_target_percent": {"constant": [50] * 5, "glide": [50, 55, 60, 65, 70]},
                "holdings": "compact population summaries and selected forensic replays; final units are not dollar wealth",
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    compare(parser.parse_args().output_dir)


if __name__ == "__main__":
    main()
