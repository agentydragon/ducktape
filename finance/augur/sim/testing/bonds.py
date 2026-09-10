"""Shared authored nominal and indexed bond cases for compiler and session tests."""

from __future__ import annotations

from decimal import Decimal

from finance.augur.model.series import InflationKey
from finance.augur.sim.scenario import BondHolding, Scenario
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import checking, taxed

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

TREASURY, MUNI, CORPORATE = "federal_us", "california", None


def bond_case(
    *,
    issuer: str | None = TREASURY,
    indexed: bool = False,
    cpi: list[float] | None = None,
    is_taxed: bool = True,
    maturity: int = NEVER_MATURES,
) -> Case:
    """One agent holding one bond, and nothing else that moves money.

    `is_taxed=False` is the scenario's way of saying "intentionally untaxed", which is what
    the pure-cashflow cases want: with a tax profile the year-end settlement lands in the
    same month as a coupon and the two net against each other.
    """

    return Case(
        scenario=bond_scenario(issuer=issuer, indexed=indexed, is_taxed=is_taxed, maturity=maturity),
        rollout_count=1,
        series={InflationKey(): levels([[Decimal(str(level)) for level in cpi or CPI_FLAT]])},
    )


def bond_scenario(
    *,
    issuer: str | None = TREASURY,
    indexed: bool = False,
    is_taxed: bool = True,
    maturity: int = NEVER_MATURES,
    account_id: str = "checking",
) -> Scenario:
    """The scenario alone, for the cases that are about authoring one rather than running it."""

    return scenario(
        checking(("alice", Decimal(100_000)), ("irs", Decimal(0))),
        initial_bonds=[
            BondHolding(
                bond_id="rung",
                agent_id="alice",
                account_id=account_id,
                issuer_jurisdiction_id=issuer,
                face_value=FACE,
                purchase_price=FACE,
                annual_coupon_rate=NOMINAL_RATE,
                coupon_period_months=6,
                purchase_month_index=0,
                maturity_month_index=maturity,
                inflation_indexed=indexed,
            )
        ],
        tax_profiles=[taxed("alice", "federal_us", "california")] if is_taxed else [],
        horizon_months=HORIZON,
    )
