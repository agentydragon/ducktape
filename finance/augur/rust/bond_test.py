"""Nominal and indexed bond acceptance through explicit monthly claim-payment actions.

The same fixed cash, income-character and redemption expectations formerly exercised the
configured runner. Product carrying-value projection remains a separate backend regression.
"""

import json
from decimal import Decimal
from itertools import pairwise

import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.rust.simulator import Action, ActionSession, DecisionActions
from finance.augur.sim.books import IncomeState
from finance.augur.sim.results import Finished, RejectedAction, Rollout
from finance.augur.sim.testing.bonds import (
    CORPORATE,
    CPI_DEFLATING,
    CPI_DOUBLING,
    CPI_FLAT,
    FACE,
    MUNI,
    NOMINAL_COUPON,
    TREASURY,
    bond_case,
    bond_scenario,
)
from finance.augur.sim.testing.case import Case, levels


def execute(case: Case) -> Rollout:
    """Pay only observed due claims in order; no native configured policy or rescue."""
    session = ActionSession(json.dumps(case.compiled_run.execution_input), "alice", [0], capture="forensic")
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id,
                        decision.observation.month,
                        [
                            Action.pay_claim(index, claim.cause_id, claim, claim.from_account, claim.amount_due)
                            for index, claim in enumerate(decision.observation.claims)
                        ],
                    )
                    for decision in batch
                ]
            )
        [result] = batch.rollouts
        return result
    finally:
        session.close()


def _quanta(amount: Decimal | int | float) -> int:
    return int(Decimal(str(amount)) * 100)


def _cash_by_month(result: Rollout) -> dict[int, int]:
    """Delta from opening to each observed closing; never pad a stopped path."""
    [cash] = result.summary.cash
    assert (cash.account.agent_id, cash.account.account_id) == ("alice", "checking")
    return {month: after - before for month, (before, after) in enumerate(pairwise(cash.values))}


def _paid(result: Rollout) -> dict[int, int]:
    return {month: delta for month, delta in _cash_by_month(result).items() if delta}


def _tax_by_jurisdiction(result: Rollout) -> dict[str, int]:
    taxes: dict[str, int] = {}
    for accrual in result.summary.tax_accruals:
        taxes[accrual.jurisdiction_id] = taxes.get(accrual.jurisdiction_id, 0) + accrual.total_tax
    return taxes


def _alice_income(result: Rollout) -> list[tuple[int, IncomeState]]:
    assert result.trace is not None
    return [
        (book.month, row)
        for book in result.trace.books
        for row in book.income
        if row.agent_id == "alice" and row.income > 0
    ]


def test_coupons_arrive_as_cash_on_their_schedule() -> None:
    """Semiannual, so months 6 and 12 and nothing in between — a bond bought today does
    not pay today, and it does not dribble monthly."""

    assert _paid(execute(bond_case(is_taxed=False))) == {6: _quanta(NOMINAL_COUPON), 12: _quanta(NOMINAL_COUPON)}


def test_a_treasury_coupon_is_federally_taxed_and_california_exempt() -> None:
    tax = _tax_by_jurisdiction(execute(bond_case(issuer=TREASURY)))

    assert tax["federal_us"] > 0
    assert tax["california"] == 0


def test_an_in_state_muni_coupon_is_exempt_everywhere() -> None:
    tax = _tax_by_jurisdiction(execute(bond_case(issuer=MUNI)))

    assert tax["federal_us"] == 0
    assert tax["california"] == 0


def test_a_corporate_coupon_is_taxed_by_both() -> None:
    """A `None` issuer is a real state, not a missing one: a non-governmental issuer that
    no jurisdiction exempts."""

    tax = _tax_by_jurisdiction(execute(bond_case(issuer=CORPORATE)))

    assert tax["federal_us"] > 0
    assert tax["california"] > 0


def test_a_coupon_accrues_as_interest_and_not_as_ordinary_income() -> None:
    """Which row it lands in is what decides whether California can reach it."""

    december = [row for month, row in _alice_income(execute(bond_case(issuer=TREASURY))) if month == 11]

    assert [row.income_source for row in december] == ["interest:federal_us"]
    assert [row.income for row in december] == [_quanta(NOMINAL_COUPON)]


def test_redemption_returns_the_face_as_cash_without_being_income() -> None:
    """Getting the principal back is a return of capital, not a coupon. At par against a
    par basis it is not a capital gain either, so it moves cash and touches no income row.
    """

    maturity = 12
    # The maturity month pays its final coupon AND returns the face.
    assert _cash_by_month(execute(bond_case(is_taxed=False, maturity=maturity)))[maturity] == _quanta(
        FACE + NOMINAL_COUPON
    )

    # Across the whole run rather than at one month, so it does not depend on which
    # snapshot a tax year turns over on: a $1M face reaching income would tower over the
    # coupons in SOME row, whichever row that is.
    income = [row.income for _, row in _alice_income(execute(bond_case(maturity=maturity)))]
    assert max(income) == _quanta(NOMINAL_COUPON)


def test_a_bond_paying_into_a_nonexistent_account_is_rejected() -> None:
    """An unresolvable account on a POSITION is a typo, not a counterparty.

    Unmodeled counterparties legitimately settle against the external account, so without
    this guard a mistyped account would quietly hand alice's own coupons to the rest of
    the world — and the books would still balance, which is exactly why the conservation
    invariant cannot catch it and an explicit rejection has to.
    """

    mistyped = Case(
        scenario=bond_scenario(account_id="brokerage"),
        rollout_count=1,
        series={InflationKey(): levels([[Decimal(str(level)) for level in CPI_FLAT]])},
    )
    with pytest.raises(ValueError, match="references unknown account alice:brokerage"):
        execute(mistyped)


def test_an_indexed_coupon_rides_the_indexed_principal() -> None:
    """CPI doubles before the month-6 coupon; both scheduled coupons use doubled principal."""

    assert _paid(execute(bond_case(indexed=True, cpi=CPI_DOUBLING, is_taxed=False))) == {
        6: _quanta(2 * NOMINAL_COUPON),
        12: _quanta(2 * NOMINAL_COUPON),
    }


def test_a_nominal_bond_ignores_the_same_cpi_path() -> None:
    """The control: same terms, same CPI, not indexed. Without it the divergence above
    could be something else the inflation path changed."""

    assert _paid(execute(bond_case(indexed=False, cpi=CPI_DOUBLING, is_taxed=False))) == {
        6: _quanta(NOMINAL_COUPON),
        12: _quanta(NOMINAL_COUPON),
    }


def test_accretion_is_income_with_no_cash_behind_it() -> None:
    """Phantom income, and the reason a TIPS loses to a muni after tax in some scenarios.

    CPI doubles at month 6, so principal rises $1M that month. That $1M is taxable
    interest the moment it accrues, and no cash moves for it.
    """

    indexed = execute(bond_case(indexed=True, cpi=CPI_DOUBLING))
    income = [row.income for _, row in _alice_income(indexed)]

    # Year one holds ONE coupon — the month-12 one is next tax year — doubled by the CPI
    # step, plus the full $1M of accretion. Accretion dwarfing the coupon is the point.
    assert max(income) == _quanta(2 * NOMINAL_COUPON + FACE)
    # And month 6 moved only the coupon in cash.
    untaxed = execute(bond_case(indexed=True, cpi=CPI_DOUBLING, is_taxed=False))
    assert _cash_by_month(untaxed)[6] == _quanta(2 * NOMINAL_COUPON)


def test_accretion_is_treasury_interest_and_inherits_its_exemption() -> None:
    """Accretion is interest on the same obligation, so 31 USC 3124 reaches it like a
    coupon. Booked as ordinary income instead, California would tax it."""

    tax = _tax_by_jurisdiction(execute(bond_case(indexed=True, cpi=CPI_DOUBLING)))

    assert tax["federal_us"] > 0
    assert tax["california"] == 0


def test_redemption_is_floored_at_par_when_prices_fall() -> None:
    """The deflation floor. CPI ends at 80% of its purchase level, so indexed principal is
    $800k — and a TIPS redeems at par, which is the promise a floor exists to make."""

    deflated = execute(bond_case(indexed=True, cpi=CPI_DEFLATING, is_taxed=False, maturity=12))

    # The final coupon rides the deflated principal; the principal itself comes back whole.
    assert _cash_by_month(deflated)[12] == _quanta(FACE + Decimal("0.8") * NOMINAL_COUPON)


def test_a_flat_cpi_makes_an_indexed_bond_behave_exactly_like_a_nominal_one() -> None:
    """Indexation with no inflation must be the identity, not merely close. Both paths are
    integer cents, so any rounding drift in the indexed branch shows here."""

    indexed = execute(bond_case(indexed=True, cpi=CPI_FLAT, is_taxed=False, maturity=12))
    nominal = execute(bond_case(indexed=False, cpi=CPI_FLAT, is_taxed=False, maturity=12))

    assert _cash_by_month(indexed) == _cash_by_month(nominal)


def test_unfunded_accretion_tax_preserves_coupon_cash_and_stops() -> None:
    result = execute(bond_case(indexed=True, cpi=CPI_DOUBLING))
    assert isinstance(result.stop, RejectedAction)
    assert result.stop.month == 12
    # Initial $100k plus two $40k coupons. Accretion added no cash; a failed
    # explicit tax payment does not consume the coupon already received.
    assert result.summary.cash[0].values[-1] == 18_000_000
    assert result.summary.ending_book.month == 13
    assert len(result.summary.cash[0].values) == 14
    assert result.summary.unpaid_claims


if __name__ == "__main__":
    pytest_bazel.main()
