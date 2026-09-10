"""Independent public-distribution controls through the common Python action session."""

import json
from decimal import Decimal
from itertools import pairwise

import pytest
import pytest_bazel

from finance.augur.model.series import SecurityDistributionKey
from finance.augur.rust.simulator import Action, ActionSession, DecisionActions
from finance.augur.sim.results import Finished, Paid, Rollout
from finance.augur.sim.testing.case import Case
from finance.augur.sim.testing.fixtures import cash_spend, checking
from finance.augur.sim.testing.security_distributions import (
    AGGREGATE,
    CALIFORNIA_MUNI,
    CORPORATE,
    CORPORATE_SHARE,
    HORIZON,
    LOSSY_PER_UNIT,
    MONTHLY_PAYOUT_QUANTA,
    PAYOUTS_BY_YEAR_END,
    PER_UNIT,
    SUB_QUANTUM_PER_UNIT,
    SYMBOL,
    TREASURY,
    TREASURY_SHARE,
    YEAR_END,
    distribution_case,
    payout_quanta,
)


def _run(case: Case) -> Rollout:
    """Receive modeled payouts and explicitly pay observed claims; never trade or retry."""
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
        assert result.stop is None
        return result
    finally:
        session.close()


def _cash_by_month(result: Rollout) -> dict[int, int]:
    [cash] = result.summary.cash
    assert (cash.account.agent_id, cash.account.account_id) == ("alice", "checking")
    return {month: after - before for month, (before, after) in enumerate(pairwise(cash.values))}


def _tax_by_jurisdiction(result: Rollout) -> dict[str, int]:
    taxes: dict[str, int] = {}
    for accrual in result.summary.tax_accruals:
        taxes[accrual.jurisdiction_id] = taxes.get(accrual.jurisdiction_id, 0) + accrual.total_tax
    return taxes


def test_the_payout_is_units_times_dollars_per_unit_every_month() -> None:
    """Monthly, unlike a semiannual coupon, and sized off units held rather than a rate on
    market value — which is why nothing in an engine needs the price to compute it."""

    assert _cash_by_month(_run(distribution_case(is_taxed=False))) == dict.fromkeys(
        range(HORIZON), MONTHLY_PAYOUT_QUANTA
    )


def test_zero_payout_months_move_no_cash_between_actual_payments() -> None:
    case = distribution_case(is_taxed=False, per_unit=Decimal(0))
    case.series[SecurityDistributionKey(symbol=SYMBOL)][0, 6] = float(PER_UNIT)
    expected = dict.fromkeys(range(HORIZON), 0)
    expected[6] = MONTHLY_PAYOUT_QUANTA
    assert _cash_by_month(_run(case)) == expected


def test_a_payout_below_one_quantum_per_unit_still_reaches_cash() -> None:
    """#5832. A per-unit figure is a RATE, and a rate has no reason to be a whole number of
    cents: 10,000 units at $0.0004 each is an ordinary $4.00 payment. Rounding the rate to
    the currency quantum before multiplying by the position sent it to zero per unit, and
    the engine then rejected the whole series as non-positive rather than paying $0.00 —
    which is the only reason anyone noticed.

    This is about the SIZE of the per-unit payout, not the length of the run: a fund at a
    low unit price reaches it at any horizon, so a thirteen-month case pins it.
    """

    case = distribution_case(is_taxed=False, per_unit=SUB_QUANTUM_PER_UNIT)
    assert _cash_by_month(_run(case)) == dict.fromkeys(range(HORIZON), payout_quanta(SUB_QUANTUM_PER_UNIT))


def test_a_payout_of_a_fractional_quantum_per_unit_is_not_rounded_away() -> None:
    """The half of #5832 that never raised: quantizing the rate first loses up to half a
    quantum PER UNIT, scaled up by the whole position.

    10,000 units at $0.0123 is $123.00. Rounded to whole cents per unit first it is
    $100.00 — a fifth of the payout gone, silently, on a series that validates fine. The
    engine multiplies before it divides, so there is nothing to round until the amount is
    money.
    """

    case = distribution_case(is_taxed=False, per_unit=LOSSY_PER_UNIT)
    assert _cash_by_month(_run(case)) == dict.fromkeys(range(HORIZON), payout_quanta(LOSSY_PER_UNIT))


def test_splitting_the_tax_character_does_not_change_what_reaches_cash() -> None:
    """The slices are a tax decomposition, not separate payouts. They share one
    destination account, so this also catches a scatter that overwrote instead of
    accumulating."""

    split = _run(distribution_case(tax_character=AGGREGATE, is_taxed=False))
    whole = _run(distribution_case(tax_character=TREASURY, is_taxed=False))

    assert _cash_by_month(split) == _cash_by_month(whole)


def test_a_holding_with_no_declared_distribution_pays_nothing() -> None:
    """Guards the cases above against passing for some unrelated reason: the same lot, the
    same sampled series, no payout declared, no cash."""

    assert set(_cash_by_month(_run(distribution_case(is_taxed=False, distributes=False))).values()) == {0}


def test_a_treasury_funds_distribution_is_federally_taxed_and_california_exempt() -> None:
    tax = _tax_by_jurisdiction(_run(distribution_case(tax_character=TREASURY)))

    assert tax["federal_us"] > 0
    assert tax["california"] == 0


def test_an_in_state_muni_funds_distribution_is_exempt_everywhere() -> None:
    tax = _tax_by_jurisdiction(_run(distribution_case(tax_character=CALIFORNIA_MUNI)))

    assert tax["federal_us"] == 0
    assert tax["california"] == 0


def test_a_mixed_fund_is_exempt_only_on_its_treasury_slice() -> None:
    """The reason the tax character is a vector. California taxes the corporate 60% and
    not the Treasury 40%, so a mixed fund owes strictly between the all-Treasury and
    all-corporate cases — a number neither single tag can produce."""

    mixed = _tax_by_jurisdiction(_run(distribution_case(tax_character=AGGREGATE)))
    treasury = _tax_by_jurisdiction(_run(distribution_case(tax_character=TREASURY)))
    corporate = _tax_by_jurisdiction(_run(distribution_case(tax_character=CORPORATE)))

    assert treasury["california"] < mixed["california"] < corporate["california"]
    # Federal taxes both slices, so the split changes nothing there.
    assert mixed["federal_us"] == treasury["federal_us"] == corporate["federal_us"]


def test_the_payout_accrues_as_interest_per_issuer_and_not_as_one_lump() -> None:
    """The slices land in their own income rows rather than summing into one, which is
    what makes the per-jurisdiction exemption computable at all."""

    result = _run(distribution_case(tax_character=AGGREGATE))
    assert result.trace is not None
    december = sorted(
        (
            row
            for book in result.trace.books
            if book.month == YEAR_END
            for row in book.income
            if row.agent_id == "alice" and row.income > 0
        ),
        key=lambda row: row.income_source,
    )
    paid = PAYOUTS_BY_YEAR_END * MONTHLY_PAYOUT_QUANTA

    assert [row.income_source for row in december] == ["interest:corporate", "interest:federal_us"]
    assert [row.income for row in december] == [int(CORPORATE_SHARE * paid), int(TREASURY_SHARE * paid)]


def test_a_distribution_on_a_pool_with_no_lots_is_rejected() -> None:
    """The distribution names ira, but this input declares only the brokerage position."""

    with pytest.raises(ValueError, match="references no lots for alice:ira:bnd"):
        _run(distribution_case(holding_account_id="ira"))


def test_a_declared_distribution_with_no_sampled_payout_series_is_rejected() -> None:
    """Named by the missing series rather than surfacing later as a non-finite payout."""

    with pytest.raises(ValueError, match="security_distribution:bnd"):
        _run(distribution_case(pays_a_series=False))


def test_current_payout_funds_an_explicit_same_month_claim() -> None:
    original = distribution_case(is_taxed=False)
    case = Case(
        scenario=original.scenario.model_copy(
            update={
                "initial_cash": checking(("alice", Decimal(0)), ("irs", Decimal(0))),
                "scheduled_obligations": [
                    cash_spend("bill", month=0, agent_id="alice", to_agent_id="irs", amount_due=Decimal(2_000))
                ],
            }
        ),
        rollout_count=1,
        series=original.series,
    )
    result = _run(case)
    assert result.summary.cash[0].values[:2] == [0, 0]
    assert result.summary.cash[0].values[-1] == 2_400_000
    [payment] = result.summary.payments
    assert payment.month == 0
    assert payment.cause_id == "bill_m0"
    assert payment.receipt.amount_paid == 200_000
    assert isinstance(payment.receipt.outcome, Paid)
    assert result.trace is not None
    [payout] = [row for row in result.trace.distributions if row.month == 0]
    assert (payout.asset_id, payout.amount, payout.issuer_jurisdiction_id) == ("bnd", 200_000, "federal_us")


if __name__ == "__main__":
    pytest_bazel.main()
