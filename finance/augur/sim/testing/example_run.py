"""The monthly-actions example as a prepared run, for tests of the prepared-input path itself.

Two stipulated prices and a synthetic tax schedule, not a forecast or statutory tax example;
the same facts `finance.augur.x.monthly_actions.run` composes straight onto a `World`.
"""

from decimal import Decimal

import numpy as np

from finance.augur.model.series import SecurityKey
from finance.augur.sim.compiler.execution import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.prepared import CompiledRun
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
