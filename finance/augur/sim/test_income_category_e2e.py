"""Sim-level e2e: one dollar, different answers per jurisdiction.

The whole point of the income-bucket axis. A CA resident is subject to two taxing
authorities, and they disagree about interest:

    instrument   federal    california
    wages        taxable    taxable
    Treasury     taxable    EXEMPT      (31 USC 3124)
    CA muni      EXEMPT     EXEMPT      (IRC 103, plus own-issue)

Before the axis, `_compute_tax_for_link` read one `ordinary_ytd[profile]` scalar for
every jurisdiction link, so the middle two rows were unrepresentable at any bracket
setting. These tests drive the table through transfers — no bond instrument needed,
which is why transfers were widened to carry `InterestIncome` first.
"""

from __future__ import annotations

import polars as pl
import pytest_bazel

from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    FilingStatus,
    InitialAccountBalance,
    InterestIncome,
    Scenario,
    ScheduledTransfer,
    TaxProfile,
    TransferIncomeCategory,
)
from finance.augur.sim.simulate import simulate

# December of the first year, so the year-end accrual has fired.
_HORIZON_MONTHS = 13
_PAYMENT_USD = 100_000.0


def _scenario(*income_categories: TransferIncomeCategory) -> Scenario:
    """One payment of `_PAYMENT_USD` to a CA resident per tag, each tagged as given."""

    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payer"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=500_000.0),
            InitialAccountBalance(
                agent_id="payer", account_id="checking", balance_usd=_PAYMENT_USD * len(income_categories)
            ),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=0,
                cause_id=f"payment_{index}",
                from_agent_id="payer",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=_PAYMENT_USD,
                income_category=income_category,
            )
            for index, income_category in enumerate(income_categories)
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=_HORIZON_MONTHS,
    )


def _tax_by_jurisdiction(income_category: TransferIncomeCategory) -> dict[str, float]:
    run = simulate(_scenario(income_category), rollout_count=1, locations={})
    accruals = run.events_log.tax_accruals
    return {
        str(row["jurisdiction_id"]): float(row["amount_usd"])
        for row in accruals.group_by("jurisdiction_id").agg(pl.col("amount_usd").sum()).to_dicts()
    }


def test_wages_are_taxed_by_both_jurisdictions() -> None:
    tax = _tax_by_jurisdiction(ORDINARY_INCOME)

    assert tax["federal_us"] > 0.0
    assert tax["california"] > 0.0


def test_treasury_interest_is_federally_taxed_and_state_exempt() -> None:
    # 31 USC 3124 bars California from taxing federal obligations. This is the row that no
    # bracket configuration could express while every link shared one income scalar.
    tax = _tax_by_jurisdiction(InterestIncome(issuer_jurisdiction_id="federal_us"))

    assert tax["federal_us"] > 0.0
    assert tax["california"] == 0.0


def test_in_state_muni_interest_is_exempt_everywhere() -> None:
    # IRC 103 excludes it federally; California exempts its own issue. "In-state" is not stored
    # anywhere — it is `issuer == california`, decided by the jurisdiction, not the instrument.
    tax = _tax_by_jurisdiction(InterestIncome(issuer_jurisdiction_id="california"))

    assert tax["federal_us"] == 0.0
    assert tax["california"] == 0.0


def test_interest_and_wages_accrue_to_separate_buckets() -> None:
    """The decoded read model keeps them apart, which is what makes the above possible.

    Both streams in one scenario, so the second source has to land in its own row: the
    bucket arithmetic is `profile * source_count + source`, and every degenerate check
    (one profile, one source) passes even when that arithmetic is wrong.
    """

    run = simulate(
        _scenario(ORDINARY_INCOME, InterestIncome(issuer_jurisdiction_id="federal_us")), rollout_count=1, locations={}
    )
    december = run.ordinary_income_ytd.filter(
        (pl.col("month_index") == 11) & (pl.col("agent_id") == "alice") & (pl.col("ordinary_income_usd") > 0.0)
    ).sort("income_source")

    assert december.get_column("income_source").to_list() == ["interest:federal_us", "ordinary"]
    assert december.get_column("ordinary_income_usd").to_list() == [_PAYMENT_USD, _PAYMENT_USD]


def test_federal_tax_on_treasury_interest_matches_tax_on_identical_wages() -> None:
    """Same dollars, same federal bracket walk — the split changes WHO taxes it, not how much.

    Guards the masked sum against quietly dropping or double-counting a bucket.
    """

    wages = _tax_by_jurisdiction(ORDINARY_INCOME)
    treasury = _tax_by_jurisdiction(InterestIncome(issuer_jurisdiction_id="federal_us"))

    assert treasury["federal_us"] == wages["federal_us"]


if __name__ == "__main__":
    pytest_bazel.main()
