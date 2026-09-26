"""Dated bonds held from month zero, composed onto a world, for the bond suites."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from finance.augur.model.series import InflationKey
from finance.augur.sim.bonds import coupon_amount_quanta
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.income_sources import income_source_sort_key
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb
from finance.augur.sim.ids import AccountId, AgentId, BondId, JurisdictionId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedBond,
    PreparedFixedAmount,
    PreparedIndexedCoupon,
    PreparedJurisdiction,
    PreparedSeries,
)
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome, TaxProfile
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
CHECKING = AccountId("checking")
HORIZON = 14
# Long enough to outlive the horizon, for the cases that are about coupons rather than
# redemption.
NEVER_MATURES = 120

FACE = Decimal(1_000_000)
NOMINAL_RATE = 0.04
# Semiannual on a $1M face at 4%, before any indexation.
NOMINAL_COUPON = Decimal(20_000)

# CPI doubles in one step at month 6 and is flat elsewhere, so the accretion is attributable
# to exactly one month. The deflating path ends below par, which is what a floor is for.
CPI_DOUBLING = [100.0] * 6 + [200.0] * (HORIZON + 1 - 6)
CPI_FLAT = [100.0] * (HORIZON + 1)
CPI_DEFLATING = [100.0] * 6 + [80.0] * (HORIZON + 1 - 6)

TREASURY, MUNI, CORPORATE = JurisdictionId("federal_us"), JurisdictionId("california"), None


def dated(
    bond_id: BondId,
    *,
    agent_id: AgentId,
    account_id: AccountId = CHECKING,
    face: Decimal,
    annual_rate: float,
    period: int,
    purchase: int = 0,
    maturity: int,
    issuer: JurisdictionId | None = None,
    indexed: bool = False,
) -> PreparedBond:
    """A bond bought at par; a nominal coupon is the annual rate's share of the face, rounded once."""
    face_quanta = int(currency_amount_to_quanta(face, quantum=QUANTUM))
    rate_ppb = rate_to_ppb(annual_rate)
    return PreparedBond(
        bond_id=bond_id,
        agent_id=agent_id,
        account_id=account_id,
        issuer_jurisdiction_id=issuer,
        face_value=face_quanta,
        purchase_price=face_quanta,
        coupon=PreparedIndexedCoupon(annual_rate_ppb=rate_ppb)
        if indexed
        else PreparedFixedAmount(
            amount=coupon_amount_quanta(
                face_quanta=face_quanta, annual_coupon_rate_ppb=rate_ppb, coupon_period_months=period
            )
        ),
        coupon_period_months=period,
        purchase_month_index=purchase,
        maturity_month_index=maturity,
    )


def cpi_series(paths: Sequence[Sequence[float]]) -> tuple[PreparedSeries, ...]:
    """One CPI level path per rollout, as the world reads it."""
    levels = np.asarray(paths, dtype=np.float64)
    rollouts, snapshots = levels.shape
    return compile_series(
        ExternalSeriesContext.from_level_blocks(
            [(InflationKey(), levels)], rollout_count=rollouts, horizon_months=snapshots - 1
        ),
        rollout_count=rollouts,
        horizon_months=snapshots - 1,
        currency_quantum=QUANTUM,
    )


def checking(*balances: tuple[AgentId, Decimal]) -> tuple[PreparedAccount, ...]:
    """Opening balances for agents holding one `checking` account each."""
    return tuple(
        PreparedAccount(
            account=AccountRef(agent_id=agent_id, account_id=AccountId("checking")),
            opening_balance=int(currency_amount_to_quanta(balance, quantum=QUANTUM)),
        )
        for agent_id, balance in balances
    )


@dataclass(frozen=True)
class Situation:
    """Accounts, the bonds held from month zero and the CPI paths that index them."""

    accounts: tuple[PreparedAccount, ...]
    bonds: tuple[PreparedBond, ...]
    horizon_months: int
    series: tuple[PreparedSeries, ...] = ()
    rollout_count: int = 1
    # Filed in the shipped federal and California law, paying `irs`.
    taxpayers: tuple[AgentId, ...] = ()


def compose(case: Situation, rollout_id: int = 0) -> World:
    """Each issuer a bond names is a jurisdiction the world knows, whether or not anyone files in it."""
    filed_in = (JurisdictionId("federal_us"), JurisdictionId("california")) if case.taxpayers else ()
    issuers = {bond.issuer_jurisdiction_id for bond in case.bonds}
    rules = {id_: load_jurisdiction(id_) for id_ in {*filed_in, *issuers} if id_ is not None}
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=tuple(
            sorted(
                {ORDINARY_INCOME, *(InterestIncome(issuer_jurisdiction_id=issuer) for issuer in issuers)},
                key=income_source_sort_key,
            )
        ),
        jurisdictions=tuple(PreparedJurisdiction(jurisdiction_id=id_, level=rules[id_].level) for id_ in sorted(rules)),
    )
    for account in case.accounts:
        world.declare_account(account)
    for agent_id in case.taxpayers:
        profile = TaxProfile(agent_id=agent_id, jurisdiction_ids=list(filed_in), tax_authority_agent_id=AgentId("irs"))
        world.track(TaxAuthority(compile_profile(profile, rules, quantum=QUANTUM)))
    for bond in case.bonds:
        world.hold(bond)
    return world


def bond_case(
    *,
    issuer: JurisdictionId | None = TREASURY,
    indexed: bool = False,
    cpi: list[float] | None = None,
    is_taxed: bool = True,
    maturity: int = NEVER_MATURES,
    account_id: AccountId = CHECKING,
) -> Situation:
    """Alice holding one $1M 4% semiannual bond and $100k cash, and nothing else that moves money.

    `is_taxed=False` is the way of saying "intentionally untaxed", which is what the
    pure-cashflow cases want: with a tax profile the year-end settlement lands in the
    same month as a coupon and the two net against each other.
    """
    return Situation(
        accounts=checking((AgentId("alice"), Decimal(100_000)), (AgentId("irs"), Decimal(0))),
        bonds=(
            dated(
                BondId("rung"),
                agent_id=AgentId("alice"),
                account_id=account_id,
                face=FACE,
                annual_rate=NOMINAL_RATE,
                period=6,
                maturity=maturity,
                issuer=issuer,
                indexed=indexed,
            ),
        ),
        horizon_months=HORIZON,
        series=cpi_series([cpi or CPI_FLAT]),
        taxpayers=(AgentId("alice"),) if is_taxed else (),
    )
