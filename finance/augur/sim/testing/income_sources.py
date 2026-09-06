"""What a taxpayer earned, kept apart from what any one jurisdiction can tax.

Income is booked to a `(taxpayer, source)` row and stays there: the source is a fact about
the money, and the exemption is a fact about the jurisdiction reading it. Keeping the two
apart is what lets a simulator answer both "how much treasury interest did alice earn" and
"how much of it can California tax" — a ledger that resolved the exemption on the way in
could answer only the second, and would report exempt income as no income at all.

Two taxpayers and two sources is the smallest shape that can catch the row arithmetic going
wrong. A row is `taxpayer * source_count + source`, so with one taxpayer the first term
vanishes and with one source the second does; either degenerate shape agrees with a plain
per-taxpayer index that is wrong. Four distinct amounts separate them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import polars as pl
import pytest

from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome, ScheduledTransfer, TransferIncomeCategory
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking, taxed
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

# 31 USC 3124 bars a state from taxing interest on federal obligations, so this source is
# federally taxable and exempt in California — the split the ledger has to keep. An in-state
# muni is exempt at both levels: IRC 103 federally, own-issue in California.
TREASURY = InterestIncome(issuer_jurisdiction_id="federal_us")
MUNI = InterestIncome(issuer_jurisdiction_id="california")
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


@dataclass(frozen=True)
class Payment:
    """One categorized payment from outside the model to a taxpayer."""

    to_agent_id: str
    source: TransferIncomeCategory
    amount: Decimal
    month: int = WAGE_MONTH


def paid_case(*payments: Payment) -> Case:
    """Every recipient a California resident with their own tax profile, paid from `payer`."""

    recipients = sorted({payment.to_agent_id for payment in payments})
    return Case(
        scenario=scenario(
            checking(
                *((agent_id, Decimal(500_000)) for agent_id in recipients),
                ("payer", sum((payment.amount for payment in payments), Decimal(0))),
                ("irs", Decimal(0)),
            ),
            horizon_months=HORIZON_MONTHS,
            scheduled_transfers=[
                ScheduledTransfer(
                    month=payment.month,
                    cause_id=f"payment-{index}",
                    from_agent_id="payer",
                    from_account_id="checking",
                    to_agent_id=payment.to_agent_id,
                    to_account_id="checking",
                    amount=payment.amount,
                    income_category=payment.source,
                )
                for index, payment in enumerate(payments)
            ],
            tax_profiles=[taxed(agent_id, "federal_us", "california") for agent_id in recipients],
        ),
        rollout_count=1,
    )


ALICE_AND_BOB = (
    Payment("alice", ORDINARY_INCOME, ALICE_WAGES),
    Payment("alice", TREASURY, ALICE_INTEREST, month=INTEREST_MONTH),
    Payment("bob", ORDINARY_INCOME, BOB_WAGES),
    Payment("bob", TREASURY, BOB_INTEREST, month=INTEREST_MONTH),
)
WAGES_ONLY = (Payment("alice", ORDINARY_INCOME, ALICE_WAGES), Payment("bob", ORDINARY_INCOME, BOB_WAGES))


def _quanta(amount: Decimal) -> int:
    return int(amount) * QUANTA_PER_UNIT


def _earned(result: SimulationResult, month: int) -> dict[tuple[str, str], int]:
    """Every taxpayer's year-to-date income by source, as of one snapshot."""

    rows = result.income.filter(pl.col("month_index") == month)
    return {(row["agent_id"], row["income_source"]): row["income_quanta"] for row in rows.to_dicts()}


def _tax_by_jurisdiction(result: SimulationResult) -> dict[str, int]:
    accruals = result.events.tax_accruals.group_by("jurisdiction_id").agg(pl.col("amount_quanta").sum())
    return dict(accruals.iter_rows())


class IncomeSourceAcceptance:
    """Inherit and supply `backend`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def backend(self) -> Backend:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def test_each_taxpayer_and_source_gets_its_own_row(self, backend: Backend) -> None:
        earned = _earned(backend(paid_case(*ALICE_AND_BOB)), YEAR_END)

        assert earned == {
            ("alice", ORDINARY_SOURCE): _quanta(ALICE_WAGES),
            ("alice", TREASURY_SOURCE): _quanta(ALICE_INTEREST),
            ("bob", ORDINARY_SOURCE): _quanta(BOB_WAGES),
            ("bob", TREASURY_SOURCE): _quanta(BOB_INTEREST),
        }

    def test_a_source_reports_zero_until_it_pays(self, backend: Backend) -> None:
        """The rows a scenario can produce exist from the start, holding nothing.

        A source that appears only once money lands in it cannot be distinguished from a
        source the scenario never had, and the difference matters to a reader deciding
        whether a taxpayer has treasury interest at all.
        """

        result = backend(paid_case(*ALICE_AND_BOB))

        assert _earned(result, BEFORE_INTEREST)[("alice", TREASURY_SOURCE)] == 0
        assert _earned(result, AFTER_INTEREST)[("alice", TREASURY_SOURCE)] == _quanta(ALICE_INTEREST)

    def test_the_ledger_is_year_to_date_and_opens_the_next_year_empty(self, backend: Backend) -> None:
        result = backend(paid_case(*ALICE_AND_BOB))

        assert _earned(result, YEAR_END)[("alice", ORDINARY_SOURCE)] == _quanta(ALICE_WAGES)
        assert set(_earned(result, NEXT_YEAR).values()) == {0}

    def test_wages_are_taxed_by_both_jurisdictions(self, backend: Backend) -> None:
        """The positive anchor for every exemption below: without it they could all pass on a
        scenario that collects nothing."""

        tax = _tax_by_jurisdiction(backend(paid_case(Payment("alice", ORDINARY_INCOME, ALICE_WAGES))))

        assert tax["federal_us"] > 0
        assert tax["california"] > 0

    def test_treasury_interest_is_federally_taxed_and_state_exempt(self, backend: Backend) -> None:
        """31 USC 3124. This is the row no bracket configuration could express while every
        jurisdiction read one shared income scalar."""

        tax = _tax_by_jurisdiction(backend(paid_case(Payment("alice", TREASURY, ALICE_WAGES))))

        assert tax["federal_us"] > 0
        assert tax["california"] == 0

    def test_in_state_muni_interest_is_exempt_everywhere(self, backend: Backend) -> None:
        """IRC 103 excludes it federally; California exempts its own issue. "In-state" is not
        stored anywhere — it is `issuer == california`, decided by the jurisdiction reading
        the row rather than by the instrument."""

        tax = _tax_by_jurisdiction(backend(paid_case(Payment("alice", MUNI, ALICE_WAGES))))

        assert tax["federal_us"] == 0
        assert tax["california"] == 0

    def test_federal_tax_on_treasury_interest_matches_tax_on_identical_wages(self, backend: Backend) -> None:
        """Same dollars, same federal bracket walk — the split changes WHO taxes it, not how
        much. Guards the masked sum against quietly dropping or double-counting a source."""

        wages = _tax_by_jurisdiction(backend(paid_case(Payment("alice", ORDINARY_INCOME, ALICE_WAGES))))
        treasury = _tax_by_jurisdiction(backend(paid_case(Payment("alice", TREASURY, ALICE_WAGES))))

        assert treasury["federal_us"] == wages["federal_us"]

    def test_taxpayers_are_independent(self, backend: Backend) -> None:
        """Two taxpayers simulated together owe exactly what each owes alone.

        Catches income crossing between taxpayers' rows: brackets are progressive, so one
        taxpayer's dollars landing in another's row push that taxpayer up a bracket and break
        the sum. A bug that merges a taxpayer's OWN sources is symmetric across them and
        survives this — the exemption case below is what pins that down.
        """

        together = _tax_by_jurisdiction(backend(paid_case(*ALICE_AND_BOB)))
        alone = [_tax_by_jurisdiction(backend(paid_case(*ALICE_AND_BOB[pair : pair + 2]))) for pair in (0, 2)]

        for jurisdiction in ("federal_us", "california"):
            assert together[jurisdiction] == sum(tax[jurisdiction] for tax in alone)

    def test_a_state_exemption_applies_to_each_taxpayer_s_own_income(self, backend: Backend) -> None:
        """With two taxpayers each holding wages and Treasuries, California's total take is
        the wages-only tax: 31 USC 3124 removes both taxpayers' interest, and nothing else.

        The exemption is a property of a `(taxpayer, source)` row, so merging a taxpayer's
        sources leaves interest in California's base — the corruption that starts at the
        second taxed agent and that no single-taxpayer scenario can show.
        """

        with_interest = _tax_by_jurisdiction(backend(paid_case(*ALICE_AND_BOB)))
        wages_only = _tax_by_jurisdiction(backend(paid_case(*WAGES_ONLY)))

        assert with_interest["california"] == wages_only["california"]
        # Federal still taxes the interest, so the equality above is not passing merely
        # because both scenarios collect nothing.
        assert with_interest["federal_us"] > wages_only["federal_us"]

    def test_interest_a_jurisdiction_exempts_is_still_income_earned(self, backend: Backend) -> None:
        """The whole point of keeping income by source rather than by jurisdiction.

        California cannot reach the coupons, and its assessment says so. That must not make
        them disappear: they are income alice earned, and the ledger reports them.
        """

        earned = _earned(backend(paid_case(*ALICE_AND_BOB)), YEAR_END)

        assert earned[("alice", TREASURY_SOURCE)] == _quanta(ALICE_INTEREST)
        assert earned[("bob", TREASURY_SOURCE)] == _quanta(BOB_INTEREST)
