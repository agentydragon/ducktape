"""Constant versus annual glide targets with canonical funding and holdings.

Three stipulated price/CPI paths, not sampled forecasts or historical evidence.
The two test securities have no payouts; tax, fees, housing and borrowing are absent.
This is a composition example, not a named retirement-study reproduction.
"""

import argparse
import json
from decimal import Decimal
from pathlib import Path

import numpy as np

from finance.augur.model.series import InflationKey, SecurityKey
from finance.augur.rust.invocation import invoke, write_prepared_input
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.scenario import (
    Agent,
    DriftBand,
    InitialAccountBalance,
    InitialLot,
    Scenario,
    ScheduledObligation,
    SeriesIndexedAmount,
    SleeveTarget,
    TargetAllocationPolicy,
)
from util.bazel.runfiles import get_required_path, own_repo_rlocation

GROWTH = SecurityKey(symbol="test-growth")
STEADY = SecurityKey(symbol="test-steady")
HORIZON_MONTHS = 60


def compare(output_dir: Path) -> None:
    """Compile once; retain both forensic populations and report paid consumption."""
    scenario = Scenario(
        agents=[Agent(agent_id="test-retiree"), Agent(agent_id="test-world")],
        initial_cash=[
            InitialAccountBalance(agent_id="test-retiree", account_id="checking", balance=Decimal(10_000)),
            InitialAccountBalance(agent_id="test-world", account_id="checking", balance=Decimal(0)),
        ],
        initial_lots=[
            InitialLot(
                lot_id=f"test-opening-{asset.symbol}",
                agent_id="test-retiree",
                account_id="checking",
                asset=asset,
                purchase_month_index=-24,
                quantity=500,
                cost_basis_per_unit=Decimal(100),
            )
            for asset in (GROWTH, STEADY)
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=month,
                obligation_id="test-consumption",
                obligation_type="cash_spend",
                agent_id="test-retiree",
                from_account_id="checking",
                to_agent_id="test-world",
                to_account_id="checking",
                amount_due=SeriesIndexedAmount(
                    base_amount=Decimal(6_000), series=InflationKey(), adjustment_period_months=12
                ),
            )
            for month in range(0, HORIZON_MONTHS, 12)
        ],
        target_allocation_policies=[
            TargetAllocationPolicy(
                agent_id="test-retiree",
                account_id="checking",
                source_account_ids=("checking",),
                sleeves=[SleeveTarget(asset=asset, weight=1) for asset in (GROWTH, STEADY)],
                cash_floor=Decimal(0),
                cash_ceiling=Decimal(10_000),
                allow_purchases=True,
                rebalancing=DriftBand(tolerance=0),
                cause_id_prefix="test-allocation",
            )
        ],
        tax_profiles=[],
        horizon_months=HORIZON_MONTHS,
    )
    month = np.arange(HORIZON_MONTHS + 1)
    growth = np.full((3, HORIZON_MONTHS + 1), 100.0)
    growth[0, 12:] = 80.0
    growth[1, 12:] = 120.0
    steady = np.full_like(growth, 100.0)
    cpi = np.broadcast_to(1.025 ** (month // 12), growth.shape)
    run = compile_run(
        scenario,
        rollout_count=3,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(GROWTH, growth), (STEADY, steady), (InflationKey(), cpi)], rollout_count=3, horizon_months=HORIZON_MONTHS
        ),
        jurisdictions={},
        locations={},
    )
    binary = get_required_path(own_repo_rlocation("finance/augur/x/allocation_glide/runner"))
    output_dir.mkdir(parents=True, exist_ok=False)
    input_path = output_dir / "execution-input.json"
    write_prepared_input(run, input_path)
    for name, step in (("constant", 0), ("glide", 5)):
        output_path = output_dir / f"{name}.json"
        output = invoke(binary=binary, input_path=input_path, output_path=output_path, arguments=[str(step)])
        for rollout in output["rollouts"]:
            paid = sum(row["amount_paid"] for row in rollout["obligations"] if row["obligation_type"] == "cash_spend")
            print(
                f"{name} path={rollout['rollout_id']}: consumption_paid=${paid / 100:.2f}; failed_month={rollout['failed_month']}"
            )
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
                "holdings": "full monthly lots and cash balances in each policy output; final units are not dollar wealth",
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
