"""Run bond-construction and spending cells through Augur's canonical household engine.

Construction functions supply unit prices and coupons; this module owns accounts,
withdrawals, funding policy, and output. No `Scenario`: each spending cell is a
`Situation` composed straight onto one `World` per path. The supplied curves are
stipulated stress paths, not sampled evidence or forecasts, so cells have no
probability weights.
"""

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import TypeAdapter

from finance.augur.model.series import SecurityDistributionKey, SecurityKey, SecuritySymbol
from finance.augur.policy.funding import fund_claims
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Record
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import (
    currency_amount_to_quanta,
    quantity_scale_for_asset,
    quantity_to_quanta,
    rate_to_ppb,
)
from finance.augur.sim.ids import AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedHoldingPool,
    PreparedLot,
    PreparedObligation,
    PreparedSeries,
)
from finance.augur.sim.results import Finished, Rollout, Stop, UnpaidClaim
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome, ObligationType
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World
from finance.augur.x.bond_policies.construction import (
    DatedConstruction,
    ProxyConstruction,
    compare_constructions,
    stipulated_curves,
)

QUANTUM = Decimal("0.01")
HOUSEHOLD = AgentId("example_household")
WORLD = "example_world"
CHECKING = "checking"
BROKERAGE = "brokerage"
STRATEGY = SecuritySymbol("EXAMPLE_BOND_STRATEGY")
INITIAL_WEALTH = Decimal(100_000)
INITIAL_UNIT_PRICE = Decimal(100)


@dataclass(frozen=True)
class Situation:
    """What every path of a spending cell shares; `compose` declares it onto one World per path."""

    series: tuple[PreparedSeries, ...]
    rollout_count: int
    horizon_months: int
    annual_spending: int  # currency quanta, claimed at months 12, 24, ... inside the horizon; zero claims nothing


def situation(construction: DatedConstruction | ProxyConstruction, *, annual_spending: Decimal) -> Situation:
    """One cell: the construction's unit prices and coupons on every path, and the household's annual claim."""
    if not annual_spending.is_finite() or annual_spending < 0:
        raise ValueError("annual_spending must be finite and nonnegative")
    prices = np.asarray(construction.price)
    rollout_count, snapshot_count = prices.shape
    horizon_months = snapshot_count - 1
    if not np.all(prices[:, 0] == float(INITIAL_UNIT_PRICE)):
        raise ValueError("construction units must start at $100 on every path")
    paths = ExternalSeriesContext.from_level_blocks(
        [
            (SecurityKey(symbol=STRATEGY), prices),
            (SecurityDistributionKey(symbol=STRATEGY), np.asarray(construction.coupon)),
        ],
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )
    return Situation(
        series=compile_series(
            paths, rollout_count=rollout_count, horizon_months=horizon_months, currency_quantum=QUANTUM
        ),
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        annual_spending=int(currency_amount_to_quanta(annual_spending, quantum=QUANTUM)),
    )


def compose(case: Situation, rollout_id: int) -> World:
    """Own strategy units, distribute coupons to checking, and sell units only to fund spending.

    Selling units proportionally liquidates the modeled investment exposure. Only the
    zero-withdrawal control literally retains every initial bond until the construction
    function sells or redeems it. Taxes and transaction costs are deliberately absent;
    the coupon distribution still names its (corporate) interest source.
    """
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME, InterestIncome(issuer_jurisdiction_id=None)),
    )
    for agent_id in (HOUSEHOLD, WORLD):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=0)
        )
    scale = quantity_scale_for_asset(SecurityKey(symbol=STRATEGY))
    world.declare_pool(
        PreparedHoldingPool(agent_id=HOUSEHOLD, account_id=BROKERAGE, asset_id=str(STRATEGY), quantity_scale=scale)
    )
    world.hold(
        PreparedLot(
            lot_id="example_initial_strategy",
            agent_id=HOUSEHOLD,
            account_id=BROKERAGE,
            asset_id=str(STRATEGY),
            purchase_month=-1,
            quantity_scale=scale,
            units=int(quantity_to_quanta(INITIAL_WEALTH / INITIAL_UNIT_PRICE, scale=scale)),
            basis=int(currency_amount_to_quanta(INITIAL_WEALTH, quantum=QUANTUM)),
        )
    )
    world.declare_distribution(
        PreparedDistribution(
            agent_id=HOUSEHOLD,
            holding_account_id=BROKERAGE,
            asset_id=str(STRATEGY),
            to_account_id=CHECKING,
            tax_character=(PreparedDistributionSlice(fraction_ppb=rate_to_ppb(1.0), issuer_jurisdiction_id=None),),
        )
    )
    if case.annual_spending > 0:
        for month in range(12, case.horizon_months, 12):
            world.track(
                Biller(
                    PreparedObligation(
                        month=month,
                        obligation_id=f"annual_spending_{month}",
                        obligation_type=ObligationType.CASH_SPEND,
                        from_account=AccountRef(agent_id=HOUSEHOLD, account_id=CHECKING),
                        to_account=AccountRef(agent_id=WORLD, account_id=CHECKING),
                        amount_due=case.annual_spending,
                        property_id=None,
                        deduction_category=None,
                        deductible_fraction_ppb=rate_to_ppb(1.0),
                    )
                )
            )
    return world


def execute(
    case: Situation, *, rollout_ids: Sequence[int], capture: Literal["summary", "dense", "forensic"] = "summary"
) -> list[Rollout]:
    """Run the monthly batch policy on selected original paths, without reinvestment."""
    session = ActionSession({id_: compose(case, id_) for id_ in rollout_ids}, HOUSEHOLD, capture=capture)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                fund_claims(batch, targets={(BROKERAGE, str(STRATEGY)): 1}, cash_account_id=CHECKING)
            )
        return batch.rollouts
    finally:
        session.close()


class Measurements(Record):
    rollout_id: int
    stop: Stop | None
    ending_mark_month: int
    ending_assets_quanta: int
    terminal_wealth_quanta: int | None
    spending_requested_quanta: int
    spending_paid_quanta: int
    attempted_spending_shortfall_quanta: int
    unpaid_claims: list[UnpaidClaim]
    tax_paid_quanta: int


class Cell(Record):
    path: str
    construction: str
    annual_spending_usd: str
    output: str
    currency_code: str
    currency_quantum: str
    measurements: Measurements


CELLS = TypeAdapter(list[Cell])


def measurements(rollout: Rollout) -> Measurements:
    """Report observed payment attempts and assets; stopped assets are not terminal wealth."""
    summary = rollout.summary
    assets = sum(row.values[-1] for row in [*summary.cash, *summary.public_holdings])
    payments = [
        row.receipt for row in summary.payments if row.target is not None and row.target.obligation_type == "cash_spend"
    ]
    requested = sum(receipt.amount_requested for receipt in payments)
    paid = sum(receipt.amount_paid for receipt in payments)
    return Measurements(
        rollout_id=rollout.rollout_id,
        stop=rollout.stop,
        ending_mark_month=summary.ending_mark_month,
        ending_assets_quanta=assets,
        terminal_wealth_quanta=assets if rollout.stop is None else None,
        spending_requested_quanta=requested,
        spending_paid_quanta=paid,
        attempted_spending_shortfall_quanta=requested - paid,
        unpaid_claims=summary.unpaid_claims,
        tax_paid_quanta=sum(row.amount_paid for row in summary.tax_payments),
    )


def run_experiment(
    *, output_dir: Path, annual_spending: tuple[Decimal, ...], trace_rollouts: tuple[int, ...] = (4, 0)
) -> None:
    """Save the supplied curves, construction traces, and canonical household outcomes."""
    if not annual_spending or any(not value.is_finite() or value < 0 for value in annual_spending):
        raise ValueError("annual_spending must contain finite nonnegative amounts")
    curves = stipulated_curves()
    if len(set(trace_rollouts)) != len(trace_rollouts) or any(not 0 <= index < len(curves) for index in trace_rollouts):
        raise ValueError("trace_rollouts must be distinct original path IDs")
    constructions = compare_constructions(np.stack(tuple(curves.values())))
    horizon_months = next(iter(curves.values())).shape[0] - 1
    output_dir.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(output_dir / "discount_curves.npz", allow_pickle=False, **curves)
    (output_dir / "config.json").write_text(
        json.dumps(
            {
                "path_source": "construction.py:stipulated_curves; deterministic, no probability weights",
                "path_names": list(curves),
                "construction_source": "construction.py:compare_constructions",
                "initial_wealth_usd": str(INITIAL_WEALTH),
                "initial_unit_price_usd": str(INITIAL_UNIT_PRICE),
                "annual_spending_usd": [str(value) for value in annual_spending],
                "withdrawal_months": list(range(12, horizon_months, 12)),
                "horizon_months": horizon_months,
                "tax_profiles": [],
                "currency_code": "USD",
                "currency_quantum": "0.01",
                "policy": "policy/funding.py:fund_claims; sales only, then full due claims in observed order",
                "decision_timing": "monthly, after current coupons and claim assembly; no retry",
                "trace_rollouts": list(trace_rollouts),
                "allow_purchases": False,
                "rebalancing": "none; sell units only to cover current due claims",
                "cash_interest": "none",
                "transaction_costs": "none",
            },
            indent=2,
        )
    )
    summaries = []
    for name, construction in constructions.items():
        strategy_dir = output_dir / name
        strategy_dir.mkdir()
        if isinstance(construction, DatedConstruction):
            np.savez_compressed(
                strategy_dir / "construction.npz",
                allow_pickle=False,
                bond_value=construction.bond_value,
                cash=construction.cash,
                coupon=construction.coupon,
                sale_proceeds=construction.sale_proceeds,
                purchases=construction.purchases,
                redemption=construction.redemption,
            )
        else:
            np.savez_compressed(
                strategy_dir / "construction.npz",
                allow_pickle=False,
                price=construction.price,
                coupon=construction.coupon,
            )
        for cell_index, spending in enumerate(annual_spending):
            cell_dir = strategy_dir / f"spending_{cell_index}"
            cell_dir.mkdir()
            case = situation(construction, annual_spending=spending)
            results = execute(case, rollout_ids=range(len(curves)))
            (cell_dir / "rollouts.json").write_text(Finished(rollouts=results).model_dump_json())
            traces = execute(case, rollout_ids=trace_rollouts, capture="forensic") if trace_rollouts else []
            if traces:
                (cell_dir / "traces.json").write_text(Finished(rollouts=traces).model_dump_json())
            for rollout, path_name in zip(results, curves, strict=True):
                summaries.append(
                    Cell(
                        path=path_name,
                        construction=name,
                        annual_spending_usd=str(spending),
                        output=str(cell_dir.relative_to(output_dir)),
                        currency_code="USD",
                        currency_quantum="0.01",
                        measurements=measurements(rollout),
                    )
                )
    (output_dir / "summary.json").write_bytes(CELLS.dump_json(summaries, indent=2))
    print(f"Saved {len(summaries)} deterministic path/construction/spending cells to {output_dir}; not probabilities.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--annual-spending", type=Decimal, nargs="+", default=(Decimal(0), Decimal(5000), Decimal(10000))
    )
    parser.add_argument("--trace-rollouts", type=int, nargs="*", default=(4, 0))
    args = parser.parse_args()
    run_experiment(
        output_dir=args.output_dir,
        annual_spending=tuple(args.annual_spending),
        trace_rollouts=tuple(args.trace_rollouts),
    )


if __name__ == "__main__":
    main()
