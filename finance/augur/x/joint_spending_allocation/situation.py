"""Two placeholder securities, an annual bill and a synthetic tax schedule, composed onto a World.

No `Scenario`: the situation is declared straight onto the world, one world per path.
"""

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from finance.augur.model.series import InflationKey, SecurityKey
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
from finance.augur.sim.scenario import ORDINARY_INCOME, FilingStatus, ObligationType, TaxProfile
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
RETIREE = AgentId("retiree")
COUNTERPARTY = "world"
TAX_AUTHORITY = "test-tax"
SECURITIES = ("test-growth", "test-steady")
_FLAT_TAX = Jurisdiction(
    jurisdiction_id="test-flat-tax",
    level=JurisdictionLevel.FEDERAL,
    ordinary_income_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.20)]},
    ltcg_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.10)]},
    standard_deduction={FilingStatus.SINGLE: Decimal(0)},
    max_capital_loss_ordinary_offset={FilingStatus.SINGLE: Decimal(0)},
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


@dataclass(frozen=True)
class Situation:
    """What every path of a cell shares; `compose` declares it onto one World per path."""

    series: tuple[PreparedSeries, ...]
    rollout_count: int
    horizon_months: int
    taxable: bool
    annual_bill: int  # currency quanta, due every twelfth month starting at month zero


def situation(
    paths: ExternalSeriesContext, *, rollout_count: int, horizon_months: int, taxable: bool, annual_bill: int = 100_000
) -> Situation:
    return Situation(
        series=compile_series(
            paths, rollout_count=rollout_count, horizon_months=horizon_months, currency_quantum=QUANTUM
        ),
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        taxable=taxable,
        annual_bill=annual_bill,
    )


def compose(situation: Situation, rollout_id: int) -> World:
    """USD 10,000 cash and 500 units of each security at USD 80 basis, bought 24 months before month zero."""
    world = World(
        MarketPath(situation.series, rollout_id, rollout_count=situation.rollout_count),
        horizon_months=situation.horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=_FLAT_TAX.jurisdiction_id, level=_FLAT_TAX.level),)
        if situation.taxable
        else (),
    )
    for name in (RETIREE, COUNTERPARTY, TAX_AUTHORITY):
        world.declare_account(
            PreparedAccount(
                account=AccountRef(agent_id=name, account_id="checking"),
                opening_balance=int(
                    currency_amount_to_quanta(Decimal(10_000 if name == RETIREE else 0), quantum=QUANTUM)
                ),
            )
        )
    if situation.taxable:
        profile = TaxProfile(
            agent_id=RETIREE,
            jurisdiction_ids=[_FLAT_TAX.jurisdiction_id],
            tax_authority_agent_id=TAX_AUTHORITY,
            prior_year_tax=Decimal(0),
        )
        world.track(TaxAuthority(compile_profile(profile, {_FLAT_TAX.jurisdiction_id: _FLAT_TAX}, quantum=QUANTUM)))
    for symbol in SECURITIES:
        scale = quantity_scale_for_asset(SecurityKey(symbol=symbol))
        world.declare_pool(
            PreparedHoldingPool(agent_id=RETIREE, account_id="checking", asset_id=symbol, quantity_scale=scale)
        )
        world.hold(
            PreparedLot(
                lot_id=f"test-opening-{symbol}",
                agent_id=RETIREE,
                account_id="checking",
                asset_id=symbol,
                purchase_month=-24,
                quantity_scale=scale,
                units=int(quantity_to_quanta(500, scale=scale)),
                basis=int(currency_amount_to_quanta(Decimal(40_000), quantum=QUANTUM)),
            )
        )
    for month in range(0, situation.horizon_months, 12):
        world.track(
            Biller(
                PreparedObligation(
                    month=month,
                    obligation_id="test-committed-bill",
                    obligation_type=ObligationType.OUTSIDE_RENT,
                    from_account=AccountRef(agent_id=RETIREE, account_id="checking"),
                    to_account=AccountRef(agent_id=COUNTERPARTY, account_id="checking"),
                    amount_due=situation.annual_bill,
                    property_id=None,
                    deduction_category=None,
                    deductible_fraction_ppb=1_000_000_000,
                )
            )
        )
    return world
