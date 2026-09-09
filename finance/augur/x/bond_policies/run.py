"""Run bond-construction and spending cells through Augur's canonical household engine.

Construction functions supply unit prices and coupons; this module owns accounts,
withdrawals, funding policy, and output. The supplied curves are stipulated stress
paths, not sampled evidence or forecasts, so cells have no probability weights.
"""

import argparse
import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl

from finance.augur.model.series import SecurityDistributionKey, SecurityKey, SecuritySymbol
from finance.augur.rust.backend import RustEngine
from finance.augur.sim.backend import CompiledRun, compile_run
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.scenario import (
    Agent,
    CashflowOnly,
    DistributionTaxSlice,
    InitialAccountBalance,
    InitialLot,
    ObligationType,
    Scenario,
    ScheduledObligation,
    SecurityDistribution,
    SleeveTarget,
    TargetAllocationPolicy,
)
from finance.augur.x.bond_policies.construction import (
    DatedConstruction,
    ProxyConstruction,
    compare_constructions,
    stipulated_curves,
)

HOUSEHOLD = "example_household"
WORLD = "example_world"
CHECKING = "checking"
BROKERAGE = "brokerage"
STRATEGY = SecuritySymbol("EXAMPLE_BOND_STRATEGY")
INITIAL_WEALTH = Decimal(100_000)
INITIAL_UNIT_PRICE = Decimal(100)


def compile_construction(
    construction: DatedConstruction | ProxyConstruction, *, annual_spending: Decimal
) -> CompiledRun:
    """Own strategy units, distribute coupons to cash, and sell units only to fund spending.

    Selling units proportionally liquidates the modeled investment exposure. Only the
    zero-withdrawal control literally retains every initial bond until the construction
    function sells or redeems it. Taxes and transaction costs are deliberately absent.
    """
    if not annual_spending.is_finite() or annual_spending < 0:
        raise ValueError("annual_spending must be finite and nonnegative")
    prices = np.asarray(construction.price)
    rollout_count, snapshot_count = prices.shape
    horizon_months = snapshot_count - 1
    if not np.all(prices[:, 0] == float(INITIAL_UNIT_PRICE)):
        raise ValueError("construction units must start at $100 on every path")
    asset = SecurityKey(symbol=STRATEGY)
    scenario = Scenario(
        agents=[Agent(agent_id=HOUSEHOLD), Agent(agent_id=WORLD)],
        initial_cash=[
            InitialAccountBalance(agent_id=agent_id, account_id=CHECKING, balance=0) for agent_id in (HOUSEHOLD, WORLD)
        ],
        initial_lots=[
            InitialLot(
                lot_id="example_initial_strategy",
                agent_id=HOUSEHOLD,
                account_id=BROKERAGE,
                asset=asset,
                purchase_month_index=-1,
                quantity=float(INITIAL_WEALTH / INITIAL_UNIT_PRICE),
                cost_basis_per_unit=INITIAL_UNIT_PRICE,
            )
        ],
        security_distributions=[
            SecurityDistribution(
                asset=asset,
                agent_id=HOUSEHOLD,
                holding_account_id=BROKERAGE,
                to_account_id=CHECKING,
                tax_character=(DistributionTaxSlice(fraction=1.0),),
            )
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=month,
                obligation_id=f"annual_spending_{month}",
                obligation_type=ObligationType.CASH_SPEND,
                agent_id=HOUSEHOLD,
                from_account_id=CHECKING,
                to_agent_id=WORLD,
                to_account_id=CHECKING,
                amount_due=annual_spending,
            )
            for month in range(12, horizon_months, 12)
            if annual_spending > 0
        ],
        target_allocation_policies=[
            TargetAllocationPolicy(
                agent_id=HOUSEHOLD,
                account_id=CHECKING,
                source_account_ids=(BROKERAGE,),
                sleeves=[SleeveTarget(asset=asset, weight=1)],
                cash_floor=Decimal(0),
                cash_ceiling=Decimal(0),
                rebalancing=CashflowOnly(),
                allow_purchases=False,
            )
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )
    return compile_run(
        scenario,
        rollout_count=rollout_count,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(asset, prices), (SecurityDistributionKey(symbol=STRATEGY), np.asarray(construction.coupon))],
            rollout_count=rollout_count,
            horizon_months=horizon_months,
        ),
        jurisdictions={},
        locations={},
    )


def run_experiment(*, output_dir: Path, annual_spending: tuple[Decimal, ...]) -> None:
    """Save exact model inputs, construction traces, and canonical household outcomes."""
    if not annual_spending or any(not value.is_finite() or value < 0 for value in annual_spending):
        raise ValueError("annual_spending must contain finite nonnegative amounts")
    curves = stipulated_curves()
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
                "cash_floor_usd": "0",
                "cash_ceiling_usd": "0",
                "allow_purchases": False,
                "rebalancing": "cashflow_only",
                "cash_interest": "none",
                "transaction_costs": "none",
            },
            indent=2,
        )
    )
    summaries = []
    engine = RustEngine()
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
            run = compile_construction(construction, annual_spending=spending)
            (cell_dir / "execution_input.json").write_text(json.dumps(run.execution_input))
            events = engine.events(run)
            metrics = engine.product_metrics(run, primary_agent_id=HOUSEHOLD)
            metric_arrays = metrics.metric_arrays()
            np.savez_compressed(
                cell_dir / "metrics.npz", allow_pickle=False, **metric_arrays, failed_month=metrics.failed_month
            )
            for frame in EVENT_FRAME_SPECS:
                events.frame(frame).write_parquet(cell_dir / f"{frame.name}.parquet")
            for rollout, path_name in enumerate(curves):
                selected = pl.col("rollout_index") == rollout
                summaries.append(
                    {
                        "path": path_name,
                        "construction": name,
                        "annual_spending_usd": str(spending),
                        "output": str(cell_dir.relative_to(output_dir)),
                        "currency_code": metrics.currency_code,
                        "currency_quantum": metrics.currency_quantum,
                        "terminal_wealth_quanta": int(metric_arrays["net_worth_quanta"][-1, rollout]),
                        "spending_paid_quanta": events.obligation_settlements.filter(selected)
                        .get_column("amount_paid_quanta")
                        .sum(),
                        "sale_proceeds_quanta": events.lot_dispositions.filter(selected)
                        .get_column("proceeds_quanta")
                        .sum(),
                        "failed_month": int(metrics.failed_month[rollout]),
                    }
                )
    (output_dir / "summary.json").write_text(json.dumps(summaries, indent=2))
    print(f"Saved {len(summaries)} deterministic path/construction/spending cells to {output_dir}; not probabilities.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--annual-spending", type=Decimal, nargs="+", default=(Decimal(0), Decimal(5000), Decimal(10000))
    )
    args = parser.parse_args()
    run_experiment(output_dir=args.output_dir, annual_spending=tuple(args.annual_spending))


if __name__ == "__main__":
    main()
