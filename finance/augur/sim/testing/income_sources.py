"""What a taxpayer earned, kept apart from what any one jurisdiction can tax.

Income is booked to a `(taxpayer, source)` row and stays there: the source is a fact about
the money, and the exemption is a fact about the jurisdiction reading it. Keeping the two
apart is what lets a simulator answer both "how much treasury interest did alice earn" and
"how much of it can California tax" — a ledger that resolved the exemption on the way in
could answer only the second, and would report exempt income as no income at all.

The scenario below is the smallest shape that can catch the row arithmetic going wrong. A
row is `taxpayer * source_count + source`, so with one taxpayer the first term vanishes and
with one source the second does; either degenerate shape agrees with a plain per-taxpayer
index that is wrong. Two of each, with four distinct amounts, separates them.
"""

from __future__ import annotations

from decimal import Decimal

import polars as pl
import pytest

from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome, ScheduledTransfer, TransferIncomeCategory
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking, taxed
from finance.augur.sim.testing.simulation_result import Backend

# 31 USC 3124 bars a state from taxing interest on federal obligations, so this source is
# federally taxable and exempt in California — the split the ledger has to keep.
TREASURY = InterestIncome(issuer_jurisdiction_id="federal_us")
TREASURY_SOURCE = "interest:federal_us"
ORDINARY_SOURCE = "ordinary"

# Through December of the first year, so the year-end assessment has fired.
HORIZON_MONTHS = 13
WAGE_MONTH, INTEREST_MONTH = 0, 3
# Four distinct amounts, one per (taxpayer, source): any collision in the row arithmetic
# produces a visibly wrong sum rather than a plausible one.
ALICE_WAGES, ALICE_INTEREST = Decimal(120_000), Decimal(45_000)
BOB_WAGES, BOB_INTEREST = Decimal(260_000), Decimal(70_000)
QUANTA_PER_UNIT = 100

# Snapshot indices: 0 is the opening state, so a payment in month `m` first shows at `m + 1`.
BEFORE_INTEREST, AFTER_INTEREST = INTEREST_MONTH, INTEREST_MONTH + 1
# The tax year closes in month 11; the ledger that year's assessment read is reported at 11,
# and the next year opens empty at 12.
YEAR_END, NEXT_YEAR = 11, 12


def _payment(index: int, agent_id: str, month: int, amount: Decimal, category: TransferIncomeCategory):
    return ScheduledTransfer(
        month=month,
        cause_id=f"payment-{index}",
        from_agent_id="payer",
        from_account_id="checking",
        to_agent_id=agent_id,
        to_account_id="checking",
        amount=amount,
        income_category=category,
    )


def two_taxpayers_paid_wages_and_treasury_interest(*, interest: bool = True) -> Case:
    """Alice and bob, each a California resident, each paid from both sources.

    `interest=False` drops the interest payments and nothing else, which is what makes the
    exemption assertion a comparison rather than a claim about an absolute figure.
    """

    wages = [
        _payment(0, "alice", WAGE_MONTH, ALICE_WAGES, ORDINARY_INCOME),
        _payment(1, "bob", WAGE_MONTH, BOB_WAGES, ORDINARY_INCOME),
    ]
    coupons = [
        _payment(2, "alice", INTEREST_MONTH, ALICE_INTEREST, TREASURY),
        _payment(3, "bob", INTEREST_MONTH, BOB_INTEREST, TREASURY),
    ]
    payments = [*wages, *coupons] if interest else wages
    return Case(
        scenario=scenario(
            checking(
                ("alice", Decimal(500_000)),
                ("bob", Decimal(500_000)),
                ("payer", sum((payment.amount for payment in payments), Decimal(0))),
                ("irs", Decimal(0)),
            ),
            horizon_months=HORIZON_MONTHS,
            scheduled_transfers=payments,
            tax_profiles=[taxed("alice", "federal_us", "california"), taxed("bob", "federal_us", "california")],
        ),
        rollout_count=1,
    )


def _earned(result, month: int) -> dict[tuple[str, str], int]:
    """Every taxpayer's year-to-date income by source, as of one snapshot."""

    rows = result.income.filter(pl.col("month_index") == month)
    return {(row["agent_id"], row["income_source"]): row["income_quanta"] for row in rows.to_dicts()}


def _tax_by_jurisdiction(result) -> dict[str, int]:
    accruals = result.events.tax_accruals.group_by("jurisdiction_id").agg(pl.col("amount_quanta").sum())
    return dict(accruals.iter_rows())


class IncomeSourceAcceptance:
    """Inherit and supply `backend`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def backend(self) -> Backend:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def test_each_taxpayer_and_source_gets_its_own_row(self, backend: Backend) -> None:
        earned = _earned(backend(two_taxpayers_paid_wages_and_treasury_interest()), YEAR_END)

        assert earned == {
            ("alice", ORDINARY_SOURCE): int(ALICE_WAGES) * QUANTA_PER_UNIT,
            ("alice", TREASURY_SOURCE): int(ALICE_INTEREST) * QUANTA_PER_UNIT,
            ("bob", ORDINARY_SOURCE): int(BOB_WAGES) * QUANTA_PER_UNIT,
            ("bob", TREASURY_SOURCE): int(BOB_INTEREST) * QUANTA_PER_UNIT,
        }

    def test_a_source_reports_zero_until_it_pays(self, backend: Backend) -> None:
        """The rows a scenario can produce exist from the start, holding nothing.

        A source that appears only once money lands in it cannot be distinguished from a
        source the scenario never had, and the difference matters to a reader deciding
        whether a taxpayer has treasury interest at all.
        """

        result = backend(two_taxpayers_paid_wages_and_treasury_interest())

        assert _earned(result, BEFORE_INTEREST)[("alice", TREASURY_SOURCE)] == 0
        assert _earned(result, AFTER_INTEREST)[("alice", TREASURY_SOURCE)] == int(ALICE_INTEREST) * QUANTA_PER_UNIT

    def test_the_ledger_is_year_to_date_and_opens_the_next_year_empty(self, backend: Backend) -> None:
        result = backend(two_taxpayers_paid_wages_and_treasury_interest())

        assert _earned(result, YEAR_END)[("alice", ORDINARY_SOURCE)] == int(ALICE_WAGES) * QUANTA_PER_UNIT
        assert set(_earned(result, NEXT_YEAR).values()) == {0}

    def test_interest_a_jurisdiction_exempts_is_still_income_earned(self, backend: Backend) -> None:
        """The whole point of keeping income by source rather than by jurisdiction.

        California cannot tax interest on federal obligations, so its assessment is the same
        whether or not the coupons were paid. That must not make the coupons disappear: they
        are income alice earned, and the ledger says so.
        """

        with_interest = backend(two_taxpayers_paid_wages_and_treasury_interest())
        without_interest = backend(two_taxpayers_paid_wages_and_treasury_interest(interest=False))

        earned = _earned(with_interest, YEAR_END)
        assert earned[("alice", TREASURY_SOURCE)] == int(ALICE_INTEREST) * QUANTA_PER_UNIT

        taxed_with, taxed_without = _tax_by_jurisdiction(with_interest), _tax_by_jurisdiction(without_interest)
        assert taxed_with["california"] == taxed_without["california"]
        assert taxed_with["federal_us"] > taxed_without["federal_us"], (
            "the same coupons are federally taxable, so the federal assessment must move"
        )
