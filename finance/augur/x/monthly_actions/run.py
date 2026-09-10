"""A due bill funded by a batch-authored sell -> pay action list.

Two stipulated prices and a synthetic tax schedule, not a forecast or statutory
tax example. The prepared document retains the complete financial assumptions.
"""

import argparse
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Literal

import numpy as np

from finance.augur.model.series import SecurityKey
from finance.augur.rust.invocation import read_prepared_input, write_prepared_input
from finance.augur.rust.simulator import ActionSession
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.results import Finished
from finance.augur.sim.scenario import (
    Agent,
    FilingStatus,
    HoldingPool,
    InitialAccountBalance,
    InitialLot,
    ObligationType,
    Scenario,
    ScheduledObligation,
    TaxProfile,
)
from finance.augur.x.monthly_actions.policy import decide


def prepare(rollout_count: int = 2, horizon_months: int = 13, *, cash_only_start: bool = False) -> CompiledRun:
    """Repeat the two stipulated price paths; this is not independent market sampling."""
    if rollout_count <= 0 or horizon_months < 13:
        raise ValueError("use positive rollouts and at least 13 months to include tax payment")
    stock = SecurityKey(symbol="example-stock")
    scenario = Scenario(
        agents=[Agent(agent_id=name) for name in ("example-household", "example-creditor", "example-tax")],
        initial_cash=[
            InitialAccountBalance(
                agent_id=name,
                account_id="checking",
                balance=Decimal(200) if cash_only_start and name == "example-household" else Decimal(0),
            )
            for name in ("example-household", "example-creditor", "example-tax")
        ],
        holding_pools=(
            [HoldingPool(agent_id="example-household", account_id="brokerage", asset=stock)] if cash_only_start else []
        ),
        initial_lots=[]
        if cash_only_start
        else [
            InitialLot(
                lot_id="example-lot",
                agent_id="example-household",
                account_id="checking",
                asset=stock,
                purchase_month_index=-24,
                quantity=2,
                cost_basis=80,
            )
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=1 if cash_only_start else 0,
                obligation_id="example-bill",
                obligation_type=ObligationType.OUTSIDE_RENT,
                agent_id="example-household",
                from_account_id="checking",
                to_agent_id="example-creditor",
                to_account_id="checking",
                amount_due=Decimal(150),
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="example-household",
                jurisdiction_ids=["example-flat-tax"],
                tax_authority_agent_id="example-tax",
                prior_year_tax=Decimal(0),
            )
        ],
        horizon_months=horizon_months,
    )
    jurisdiction = Jurisdiction(
        jurisdiction_id="example-flat-tax",
        level=JurisdictionLevel.FEDERAL,
        ordinary_income_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.20)]},
        ltcg_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.10)]},
        standard_deduction={FilingStatus.SINGLE: Decimal(0)},
        max_capital_loss_ordinary_offset={FilingStatus.SINGLE: Decimal(0)},
    )
    prices = np.repeat(np.resize(np.array([100.0, 50.0]), rollout_count)[:, None], horizon_months + 1, axis=1)
    if cash_only_start:
        prices[:, 1:] *= 1.2
    return compile_run(
        scenario,
        rollout_count=rollout_count,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(stock, prices)], rollout_count=rollout_count, horizon_months=horizon_months
        ),
        jurisdictions={jurisdiction.jurisdiction_id: jurisdiction},
        locations={},
    )


def execute(
    input_path: Path, output_path: Path, rollout_ids: Sequence[int], capture: Literal["summary", "dense", "forensic"]
) -> Finished:
    """Own the monthly Python loop over one retained action session and write its results."""
    session = ActionSession(read_prepared_input(input_path), "example-household", list(rollout_ids), capture=capture)
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
    compiled = prepare(rollout_count, horizon_months, cash_only_start=cash_only_start)
    output_dir.mkdir(parents=True, exist_ok=False)
    input_path = output_dir / "execution-input.json"
    write_prepared_input(compiled, input_path)
    ids = range(rollout_count) if rollout_ids is None else rollout_ids
    output = execute(input_path, output_dir / "outcomes.json", ids, capture)
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
