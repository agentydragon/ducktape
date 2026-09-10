"""A due bill funded by a batch-authored sell -> pay action list.

Two stipulated prices and a synthetic tax schedule, not a forecast or statutory
tax example. The prepared document retains the complete financial assumptions.
"""

import argparse
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from finance.augur.model.series import SecurityKey
from finance.augur.rust.invocation import invoke, write_prepared_input
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.scenario import (
    Agent,
    FilingStatus,
    InitialAccountBalance,
    InitialLot,
    ObligationType,
    Scenario,
    ScheduledObligation,
    TaxProfile,
)
from util.bazel.runfiles import get_required_path, own_repo_rlocation


def run_example(output_dir: Path, rollout_ids: Sequence[int] = (0, 1)) -> dict[str, Any]:
    stock = SecurityKey(symbol="example-stock")
    scenario = Scenario(
        agents=[Agent(agent_id=name) for name in ("example-household", "example-creditor", "example-tax")],
        initial_cash=[
            InitialAccountBalance(agent_id=name, account_id="checking", balance=Decimal(0))
            for name in ("example-household", "example-creditor", "example-tax")
        ],
        initial_lots=[
            InitialLot(
                lot_id="example-lot",
                agent_id="example-household",
                account_id="checking",
                asset=stock,
                purchase_month_index=-24,
                quantity=2,
                cost_basis_per_unit=Decimal(40),
            )
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
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
        horizon_months=13,
    )
    jurisdiction = Jurisdiction(
        jurisdiction_id="example-flat-tax",
        level=JurisdictionLevel.FEDERAL,
        ordinary_income_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.20)]},
        ltcg_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.10)]},
        standard_deduction={FilingStatus.SINGLE: Decimal(0)},
        max_capital_loss_ordinary_offset={FilingStatus.SINGLE: Decimal(0)},
    )
    compiled = compile_run(
        scenario,
        rollout_count=2,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(stock, np.repeat(np.array([[100.0], [50.0]]), 14, axis=1))], rollout_count=2, horizon_months=13
        ),
        jurisdictions={jurisdiction.jurisdiction_id: jurisdiction},
        locations={},
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    input_path = output_dir / "execution-input.json"
    write_prepared_input(compiled, input_path)
    output = invoke(
        binary=get_required_path(own_repo_rlocation("finance/augur/x/monthly_actions/runner")),
        input_path=input_path,
        output_path=output_dir / "outcomes.json",
        arguments=[str(rollout_id) for rollout_id in rollout_ids],
    )
    for rollout in output["rollouts"]:
        financial = rollout["financial"]
        paid = sum(row["amount_paid"] for row in financial["obligations"])
        print(
            f"path={financial['rollout_id']}: paid=${paid / 100:.2f}; "
            f"receipts={len(rollout['receipts'])}; stop={rollout['stop']}"
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rollout", type=int, action="append")
    args = parser.parse_args()
    run_example(args.output_dir, (0, 1) if args.rollout is None else args.rollout)


if __name__ == "__main__":
    main()
