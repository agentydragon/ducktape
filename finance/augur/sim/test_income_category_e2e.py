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

from collections.abc import Sequence
from dataclasses import dataclass

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

_TREASURY = InterestIncome(issuer_jurisdiction_id="federal_us")

# Four distinct amounts, one per (agent, source), so that any row collision in the bucket
# arithmetic produces a visibly wrong sum rather than a plausible one.
_ALICE_WAGES = 120_000.0
_ALICE_INTEREST = 45_000.0
_BOB_WAGES = 260_000.0
_BOB_INTEREST = 70_000.0


@dataclass(frozen=True)
class _Payment:
    to_agent_id: str
    income_category: TransferIncomeCategory
    amount_usd: float


def _scenario_for(payments: Sequence[_Payment]) -> Scenario:
    """Every recipient is a CA resident with their own tax profile, paid in month 0."""

    recipients = sorted({payment.to_agent_id for payment in payments})
    return Scenario(
        agents=[Agent(agent_id=agent_id) for agent_id in (*recipients, "payer", "irs")],
        initial_cash=[
            *(
                InitialAccountBalance(agent_id=agent_id, account_id="checking", balance_usd=500_000.0)
                for agent_id in recipients
            ),
            InitialAccountBalance(
                agent_id="payer", account_id="checking", balance_usd=sum(payment.amount_usd for payment in payments)
            ),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=0,
                cause_id=f"payment_{index}",
                from_agent_id="payer",
                from_account_id="checking",
                to_agent_id=payment.to_agent_id,
                to_account_id="checking",
                amount_usd=payment.amount_usd,
                income_category=payment.income_category,
            )
            for index, payment in enumerate(payments)
        ],
        tax_profiles=[
            TaxProfile(
                agent_id=agent_id,
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
            for agent_id in recipients
        ],
        horizon_months=_HORIZON_MONTHS,
    )


def _scenario(*income_categories: TransferIncomeCategory) -> Scenario:
    return _scenario_for([_Payment("alice", category, _PAYMENT_USD) for category in income_categories])


def _tax_for(payments: Sequence[_Payment]) -> dict[str, float]:
    run = simulate(_scenario_for(payments), rollout_count=1, locations={})
    accruals = run.events_log.tax_accruals
    return {
        str(row["jurisdiction_id"]): float(row["amount_usd"])
        for row in accruals.group_by("jurisdiction_id").agg(pl.col("amount_usd").sum()).to_dicts()
    }


def _tax_by_jurisdiction(income_category: TransferIncomeCategory) -> dict[str, float]:
    return _tax_for([_Payment("alice", income_category, _PAYMENT_USD)])


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

    run = simulate(_scenario(ORDINARY_INCOME, _TREASURY), rollout_count=1, locations={})
    december = run.ordinary_income_ytd.filter(
        (pl.col("month_index") == 11) & (pl.col("agent_id") == "alice") & (pl.col("ordinary_income_usd") > 0.0)
    ).sort("income_source")

    assert december.get_column("income_source").to_list() == ["interest:federal_us", "ordinary"]
    assert december.get_column("ordinary_income_usd").to_list() == [_PAYMENT_USD, _PAYMENT_USD]


def _december_income(payments: Sequence[_Payment]) -> list[tuple[str, str, float]]:
    run = simulate(_scenario_for(payments), rollout_count=1, locations={})
    december = run.ordinary_income_ytd.filter(
        (pl.col("month_index") == 11) & (pl.col("ordinary_income_usd") > 0.0)
    ).sort("agent_id", "income_source")
    return [
        (str(row["agent_id"]), str(row["income_source"]), float(row["ordinary_income_usd"]))
        for row in december.to_dicts()
    ]


def test_each_taxed_agent_gets_its_own_source_rows() -> None:
    """Two profiles AND two sources — the only shape where the row arithmetic can be wrong.

    A row is `profile * source_count + source`. With one profile the term vanishes and with
    one source the offset does, so both degenerate shapes agree with a plain `profile` index
    even though it is wrong. Only this shape separates them: under `profile`, bob's two
    payments would land in alice's two rows.
    """

    assert _december_income(
        [
            _Payment("alice", ORDINARY_INCOME, _ALICE_WAGES),
            _Payment("alice", _TREASURY, _ALICE_INTEREST),
            _Payment("bob", ORDINARY_INCOME, _BOB_WAGES),
            _Payment("bob", _TREASURY, _BOB_INTEREST),
        ]
    ) == [
        ("alice", "interest:federal_us", _ALICE_INTEREST),
        ("alice", "ordinary", _ALICE_WAGES),
        ("bob", "interest:federal_us", _BOB_INTEREST),
        ("bob", "ordinary", _BOB_WAGES),
    ]


def test_agents_are_independent_taxpayers() -> None:
    """Simulating two agents together owes exactly what simulating each alone owes.

    Catches income crossing between agents' rows: brackets are progressive, so one agent's
    dollars landing in another's bucket pushes that agent up a bracket and breaks the sum.
    A bug that merges an agent's OWN sources is symmetric across agents and survives this —
    `test_state_exemption_applies_to_each_agents_own_income` is what pins that down.
    """

    alice = [_Payment("alice", ORDINARY_INCOME, _ALICE_WAGES), _Payment("alice", _TREASURY, _ALICE_INTEREST)]
    bob = [_Payment("bob", ORDINARY_INCOME, _BOB_WAGES), _Payment("bob", _TREASURY, _BOB_INTEREST)]

    together = _tax_for([*alice, *bob])
    alice_alone = _tax_for(alice)
    bob_alone = _tax_for(bob)

    assert together["federal_us"] == alice_alone["federal_us"] + bob_alone["federal_us"]
    assert together["california"] == alice_alone["california"] + bob_alone["california"]


def test_state_exemption_applies_to_each_agents_own_income() -> None:
    """With two agents each holding wages and Treasuries, California's total take is the
    wages-only tax: 31 USC 3124 removes both agents' interest, and nothing else.

    The exemption is a property of a (profile, source) row, so merging an agent's sources
    leaves interest in California's base — which is exactly the corruption that starts at
    the second taxed agent and that no single-agent scenario can show.
    """

    with_interest = _tax_for(
        [
            _Payment("alice", ORDINARY_INCOME, _ALICE_WAGES),
            _Payment("alice", _TREASURY, _ALICE_INTEREST),
            _Payment("bob", ORDINARY_INCOME, _BOB_WAGES),
            _Payment("bob", _TREASURY, _BOB_INTEREST),
        ]
    )
    wages_only = _tax_for(
        [_Payment("alice", ORDINARY_INCOME, _ALICE_WAGES), _Payment("bob", ORDINARY_INCOME, _BOB_WAGES)]
    )

    assert with_interest["california"] == wages_only["california"]
    # The interest is not exempt everywhere — federal still taxes it, so the comparison above
    # is not passing merely because both scenarios collect nothing.
    assert with_interest["federal_us"] > wages_only["federal_us"]


def test_federal_tax_on_treasury_interest_matches_tax_on_identical_wages() -> None:
    """Same dollars, same federal bracket walk — the split changes WHO taxes it, not how much.

    Guards the masked sum against quietly dropping or double-counting a bucket.
    """

    wages = _tax_by_jurisdiction(ORDINARY_INCOME)
    treasury = _tax_by_jurisdiction(InterestIncome(issuer_jurisdiction_id="federal_us"))

    assert treasury["federal_us"] == wages["federal_us"]


if __name__ == "__main__":
    pytest_bazel.main()
