"""Sim-level e2e for phase-1 bonds: coupons are cash, and they are interest.

This is what the income-bucket axis was built for. A California resident holding a
Treasury owes federal tax on the coupon and nothing to California (31 USC 3124); holding a
California municipal bond, they owe neither (IRC 103, plus own-issue). Before the axis
every jurisdiction read one shared income scalar, so neither row was expressible at any
bracket setting.
"""

from __future__ import annotations

from itertools import pairwise

import polars as pl
import pytest_bazel

from finance.augur.sim.scenario import Agent, BondHolding, FilingStatus, InitialAccountBalance, Scenario, TaxProfile
from finance.augur.sim.simulate import simulate

_HORIZON_MONTHS = 13
_FACE_USD = 1_000_000.0
_ANNUAL_RATE = 0.05
# Two semiannual coupons land inside the first year: 5% on 1M, half a year each.
_COUPON_USD = 25_000.0


def _scenario(*, issuer: str | None, maturity_month_index: int = 120, taxed: bool = True) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=50_000.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_bonds=[
            BondHolding(
                bond_id="ladder_rung",
                agent_id="alice",
                issuer_jurisdiction_id=issuer,
                face_value_usd=_FACE_USD,
                purchase_price_usd=_FACE_USD,
                annual_coupon_rate=_ANNUAL_RATE,
                coupon_period_months=6,
                purchase_month_index=0,
                maturity_month_index=maturity_month_index,
            )
        ],
        # An empty list is the scenario's way of saying "intentionally untaxed", which is what
        # the pure-cashflow tests want: with a tax profile, the year-end settlement leaves the
        # account in the same month a coupon arrives and the two net against each other.
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ]
        if taxed
        else [],
        horizon_months=_HORIZON_MONTHS,
    )


def _tax_by_jurisdiction(*, issuer: str | None) -> dict[str, float]:
    run = simulate(_scenario(issuer=issuer), rollout_count=1, locations={})
    return {
        str(row["jurisdiction_id"]): float(row["amount_usd"])
        for row in run.events_log.tax_accruals.group_by("jurisdiction_id").agg(pl.col("amount_usd").sum()).to_dicts()
    }


def _alice_cash_by_month(*, issuer: str | None, maturity_month_index: int = 120) -> dict[int, float]:
    """Cash change attributable to each simulated month.

    Row 0 of the balance frame is the opening balance and row `m + 1` is the balance once
    month `m` has run, so a cashflow in month `m` is the delta INTO row `m + 1`. Keying the
    result by month rather than by row keeps that offset in one place.
    """

    run = simulate(
        _scenario(issuer=issuer, maturity_month_index=maturity_month_index, taxed=False), rollout_count=1, locations={}
    )
    balances = [
        float(value)
        for value in run.cash_balances.filter(pl.col("agent_id") == "alice")
        .sort("month_index")
        .get_column("balance_usd")
        .to_list()
    ]
    return {month: round(after - before, 2) for month, (before, after) in enumerate(pairwise(balances))}


def test_coupons_arrive_as_cash_on_their_schedule() -> None:
    """Semiannual, so months 6 and 12 and nothing in between — a bond bought today does not
    pay today, and it does not dribble monthly."""

    deltas = _alice_cash_by_month(issuer="federal_us")

    assert {month: delta for month, delta in deltas.items() if delta} == {6: _COUPON_USD, 12: _COUPON_USD}


def test_treasury_coupon_is_federally_taxed_and_california_exempt() -> None:
    tax = _tax_by_jurisdiction(issuer="federal_us")

    assert tax["federal_us"] > 0.0
    assert tax["california"] == 0.0


def test_in_state_muni_coupon_is_exempt_everywhere() -> None:
    tax = _tax_by_jurisdiction(issuer="california")

    assert tax["federal_us"] == 0.0
    assert tax["california"] == 0.0


def test_corporate_coupon_is_taxed_by_both() -> None:
    """`None` issuer is a real state — a non-governmental issuer no jurisdiction exempts."""

    tax = _tax_by_jurisdiction(issuer=None)

    assert tax["federal_us"] > 0.0
    assert tax["california"] > 0.0


def test_coupon_accrues_as_interest_not_ordinary_income() -> None:
    run = simulate(_scenario(issuer="federal_us"), rollout_count=1, locations={})
    december = run.ordinary_income_ytd.filter(
        (pl.col("month_index") == 11) & (pl.col("agent_id") == "alice") & (pl.col("ordinary_income_usd") > 0.0)
    )

    assert december.get_column("income_source").to_list() == ["interest:federal_us"]
    assert december.get_column("ordinary_income_usd").to_list() == [_COUPON_USD]


def test_redemption_returns_the_face_as_cash_without_being_income() -> None:
    """Getting the principal back is a return of capital, not a coupon. At par against a par
    basis it is not a capital gain either, so it must move cash and touch no tax tensor."""

    maturity = 12
    deltas = _alice_cash_by_month(issuer="federal_us", maturity_month_index=maturity)

    # The maturity month pays its final coupon AND returns the face.
    assert deltas[maturity] == _FACE_USD + _COUPON_USD

    # Asserted across the whole run rather than at one month, so it does not depend on which
    # row a tax year turns over on: a $1M face reaching income would tower over the $25k
    # coupons in SOME row, whichever row that is.
    run = simulate(_scenario(issuer="federal_us", maturity_month_index=maturity), rollout_count=1, locations={})
    income = run.ordinary_income_ytd.filter(pl.col("agent_id") == "alice").get_column("ordinary_income_usd").to_list()

    assert max(income) == _COUPON_USD


if __name__ == "__main__":
    pytest_bazel.main()
