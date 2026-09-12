"""A due bill funded by a batch-authored sell -> pay action list.

Two stipulated prices and a synthetic tax schedule, not a forecast or statutory
tax example. No `Scenario`: the situation is declared straight onto the world,
one world per path.
"""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import numpy as np

from finance.augur.model.series import SecurityKey
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.ids import AgentId
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLot,
    PreparedObligation,
    PreparedSeries,
)
from finance.augur.sim.results import Finished
from finance.augur.sim.scenario import ORDINARY_INCOME, FilingStatus, ObligationType, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World
from finance.augur.x.monthly_actions.policy import decide

QUANTUM = Decimal("0.01")
HOUSEHOLD = AgentId("example-household")
CREDITOR = "example-creditor"
TAX_AUTHORITY = "example-tax"
STOCK = SecurityKey(symbol="example-stock")
_FLAT_TAX = Jurisdiction(
    jurisdiction_id="example-flat-tax",
    level=JurisdictionLevel.FEDERAL,
    ordinary_income_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.20)]},
    ltcg_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.10)]},
    standard_deduction={FilingStatus.SINGLE: Decimal(0)},
    max_capital_loss_ordinary_offset={FilingStatus.SINGLE: Decimal(0)},
)


@dataclass(frozen=True)
class Situation:
    """What every path shares; `compose` declares it onto one World per path."""

    series: tuple[PreparedSeries, ...]
    rollout_count: int
    horizon_months: int
    cash_only_start: bool


def situation(rollout_count: int = 2, horizon_months: int = 13, *, cash_only_start: bool = False) -> Situation:
    """Repeat the two stipulated price paths; this is not independent market sampling."""
    if rollout_count <= 0 or horizon_months < 13:
        raise ValueError("use positive rollouts and at least 13 months to include tax payment")
    prices = np.repeat(np.resize(np.array([100.0, 50.0]), rollout_count)[:, None], horizon_months + 1, axis=1)
    if cash_only_start:
        prices[:, 1:] *= 1.2
    paths = ExternalSeriesContext.from_level_blocks(
        [(STOCK, prices)], rollout_count=rollout_count, horizon_months=horizon_months
    )
    return Situation(
        series=compile_series(
            paths, rollout_count=rollout_count, horizon_months=horizon_months, currency_quantum=QUANTUM
        ),
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        cash_only_start=cash_only_start,
    )


def compose(case: Situation, rollout_id: int) -> World:
    """No cash and two shares at USD 40 basis bought 24 months before month zero, with a USD 150 bill due at month 0.

    The cash-only opening instead holds USD 200, declares an empty brokerage pool and
    receives the bill at month 1. The creditor and tax authority are scripted sinks.
    """
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=_FLAT_TAX.jurisdiction_id, level=_FLAT_TAX.level),),
    )
    for name in (HOUSEHOLD, CREDITOR, TAX_AUTHORITY):
        world.declare_account(
            PreparedAccount(
                account=AccountRef(agent_id=name, account_id="checking"),
                opening_balance=int(
                    currency_amount_to_quanta(
                        Decimal(200) if case.cash_only_start and name == HOUSEHOLD else Decimal(0), quantum=QUANTUM
                    )
                ),
            )
        )
    profile = TaxProfile(
        agent_id=HOUSEHOLD,
        jurisdiction_ids=[_FLAT_TAX.jurisdiction_id],
        tax_authority_agent_id=TAX_AUTHORITY,
        prior_year_tax=Decimal(0),
    )
    world.track(TaxAuthority(compile_profile(profile, {_FLAT_TAX.jurisdiction_id: _FLAT_TAX}, quantum=QUANTUM)))
    scale = quantity_scale_for_asset(STOCK)
    world.declare_pool(
        PreparedHoldingPool(
            agent_id=HOUSEHOLD,
            account_id="brokerage" if case.cash_only_start else "checking",
            asset_id=str(STOCK.symbol),
            quantity_scale=scale,
        )
    )
    if not case.cash_only_start:
        world.hold(
            PreparedLot(
                lot_id="example-lot",
                agent_id=HOUSEHOLD,
                account_id="checking",
                asset_id=str(STOCK.symbol),
                purchase_month=-24,
                quantity_scale=scale,
                units=int(quantity_to_quanta(2, scale=scale)),
                basis=int(currency_amount_to_quanta(Decimal(80), quantum=QUANTUM)),
            )
        )
    world.track(
        Biller(
            PreparedObligation(
                month=1 if case.cash_only_start else 0,
                obligation_id="example-bill",
                obligation_type=ObligationType.OUTSIDE_RENT,
                from_account=AccountRef(agent_id=HOUSEHOLD, account_id="checking"),
                to_account=AccountRef(agent_id=CREDITOR, account_id="checking"),
                amount_due=int(currency_amount_to_quanta(Decimal(150), quantum=QUANTUM)),
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=1_000_000_000,
            )
        )
    )
    return world


def execute(
    case: Situation, output_path: Path, rollout_ids: Sequence[int], capture: Literal["summary", "dense", "forensic"]
) -> Finished:
    """Own the monthly Python loop over one retained action session and write its results."""
    session = ActionSession({id_: compose(case, id_) for id_ in rollout_ids}, HOUSEHOLD, capture=capture)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(decide(batch))
        output = batch
    finally:
        session.close()
    output_path.write_text(output.model_dump_json())
    return output


def run_example(
    output_dir: Path,
    rollout_ids: Sequence[int] | None = None,
    capture: Literal["summary", "dense", "forensic"] = "forensic",
    *,
    rollout_count: int = 2,
    horizon_months: int = 13,
    cash_only_start: bool = False,
) -> Finished:
    case = situation(rollout_count, horizon_months, cash_only_start=cash_only_start)
    output_dir.mkdir(parents=True, exist_ok=False)
    ids = range(rollout_count) if rollout_ids is None else rollout_ids
    output = execute(case, output_dir / "outcomes.json", ids, capture)
    stopped = sum(rollout.stop is not None for rollout in output.rollouts)
    print(f"paths={len(output.rollouts)}; stopped={stopped}; capture={capture}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rollout", type=int, action="append")
    parser.add_argument(
        "--cash-only-start", action="store_true", help="Buy an unheld declared asset before the bill arrives."
    )
    parser.add_argument("--capture", choices=("summary", "dense", "forensic"), default="forensic")
    parser.add_argument("--rollouts", type=int, default=2)
    parser.add_argument("--horizon-months", type=int, default=13)
    args = parser.parse_args()
    run_example(
        args.output_dir,
        args.rollout,
        args.capture,
        cash_only_start=args.cash_only_start,
        rollout_count=args.rollouts,
        horizon_months=args.horizon_months,
    )


if __name__ == "__main__":
    main()
