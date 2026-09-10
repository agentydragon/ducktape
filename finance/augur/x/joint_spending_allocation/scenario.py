"""Two placeholder securities and a synthetic tax schedule on caller-supplied paths."""

from decimal import Decimal

import numpy as np

from finance.augur.model.series import InflationKey, SecurityKey
from finance.augur.sim.backend import CompiledRun, compile_run
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


def sample(*, horizon_months: int) -> ExternalSeriesContext:
    """Three deterministic stress cases, not draws from an estimated distribution."""
    growth = np.full((3, horizon_months + 1), 100.0)
    for month, prices in ((12, (50, 150, 100)), (24, (25, 225, 110)), (36, (80, 180, 100))):
        growth[:, month:] = np.asarray(prices)[:, None]
    steady = np.full_like(growth, 100.0)
    cpi = np.broadcast_to(1.05 ** (np.arange(horizon_months + 1) // 12), growth.shape)
    return ExternalSeriesContext.from_level_blocks(
        [
            (SecurityKey(symbol="test-growth"), growth),
            (SecurityKey(symbol="test-steady"), steady),
            (InflationKey(), cpi),
        ],
        rollout_count=3,
        horizon_months=horizon_months,
    )


def prepare(
    external_series: ExternalSeriesContext, *, rollout_count: int, horizon_months: int, taxable: bool
) -> CompiledRun:
    jurisdiction = Jurisdiction(
        jurisdiction_id="test-flat-tax",
        level=JurisdictionLevel.FEDERAL,
        ordinary_income_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.20)]},
        ltcg_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.10)]},
        standard_deduction={FilingStatus.SINGLE: Decimal(0)},
        max_capital_loss_ordinary_offset={FilingStatus.SINGLE: Decimal(0)},
    )
    scenario = Scenario(
        agents=[Agent(agent_id=name) for name in ("retiree", "world", "test-tax")],
        initial_cash=[
            InitialAccountBalance(
                agent_id=name, account_id="checking", balance=Decimal(10_000 if name == "retiree" else 0)
            )
            for name in ("retiree", "world", "test-tax")
        ],
        initial_lots=[
            InitialLot(
                lot_id=f"test-opening-{symbol}",
                agent_id="retiree",
                account_id="checking",
                asset=SecurityKey(symbol=symbol),
                purchase_month_index=-24,
                quantity=500,
                cost_basis=40000,
            )
            for symbol in ("test-growth", "test-steady")
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=month,
                obligation_id="test-committed-bill",
                obligation_type=ObligationType.OUTSIDE_RENT,
                agent_id="retiree",
                from_account_id="checking",
                to_agent_id="world",
                to_account_id="checking",
                amount_due=Decimal(1_000),
            )
            for month in range(0, horizon_months, 12)
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="retiree",
                jurisdiction_ids=[jurisdiction.jurisdiction_id],
                tax_authority_agent_id="test-tax",
                prior_year_tax=Decimal(0),
            )
        ]
        if taxable
        else [],
        horizon_months=horizon_months,
    )
    return compile_run(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        jurisdictions={jurisdiction.jurisdiction_id: jurisdiction} if taxable else {},
        locations={},
    )
