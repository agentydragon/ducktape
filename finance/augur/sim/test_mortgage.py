"""Exact servicing facts, independently checked against closed-form amortization."""

from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal, localcontext

import pytest
import pytest_bazel

from finance.augur.sim.books import AccountRef
from finance.augur.sim.mortgage import Mortgage, MortgageTerms


def _terms(*, principal: int = 40_000_000, rate: int = 60_000_000, months: int = 360) -> MortgageTerms:
    return MortgageTerms(
        liability_id="loan",
        property_id="house",
        borrower=AccountRef(agent_id="owner", account_id="cash"),
        lender=AccountRef(agent_id="bank", account_id="payments"),
        origination_month=0,
        origination_principal=principal,
        annual_interest_rate_ppb=rate,
        term_months=months,
    )


@pytest.mark.parametrize(
    ("principal", "rate", "months"),
    [(40_000_000, 60_000_000, 360), (12_345, 70_000_001, 37), (1, 0, 2), (25, 120_000_000, 2), (100, 0, 3)],
)
def test_fixed_payment_matches_independent_high_precision_annuity(principal: int, rate: int, months: int) -> None:
    # This oracle has neither the implementation's 1e18 grid nor iterative
    # discount rounding; its independent closed form pins ordinary loan terms.
    with localcontext() as context:
        context.prec = 60
        monthly_rate = Decimal(rate) / 12_000_000_000
        expected = (
            Decimal(principal) * monthly_rate / (1 - (1 + monthly_rate) ** -months)
            if rate
            else Decimal(principal) / months
        ).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    mortgage = Mortgage(_terms(principal=principal, rate=rate, months=months))
    assert mortgage.monthly_payment == int(expected)


def test_first_payment_and_external_principal_authority() -> None:
    mortgage = Mortgage(_terms())
    opening = mortgage.observe(40_000_000)
    assert mortgage.monthly_payment == 239_820  # $400k, 6%, 30 years, currency cents.
    assert mortgage.payment(0, 40_000_000, 0) is None
    payment = mortgage.payment(1, 40_000_000, 250_000_000)
    assert payment is not None
    assert (payment.interest, payment.principal, payment.total, payment.rental_interest) == (
        200_000,
        39_820,
        239_820,
        50_000,
    )
    assert mortgage.observe(40_000_000) == opening  # Quoting did not book a payment.
    mortgage.record_payment(payment, principal_after=39_960_180)
    observed = mortgage.observe(39_960_180)
    assert observed.principal == 39_960_180
    assert (observed.interest_paid_ytd, observed.rental_interest_paid_ytd) == (200_000, 50_000)
    # A separately posted principal reduction changes the next quote directly;
    # the entity must not continue amortizing its own copy of the old balance.
    after_extra_principal = mortgage.payment(2, 20_000_000, 0)
    assert after_extra_principal is not None
    assert (after_extra_principal.interest, after_extra_principal.principal) == (100_000, 139_820)


@pytest.mark.parametrize(("months", "payments"), [(3, [33, 33, 33, 1]), (6, [17, 17, 17, 17, 17, 15])])
def test_zero_rate_final_payment_clamps_and_rounding_residual_remains_due(months: int, payments: list[int]) -> None:
    mortgage = Mortgage(_terms(principal=100, rate=0, months=months))
    principal = 100
    actual = []
    for month in range(1, len(payments) + 1):
        payment = mortgage.payment(month, principal, 1_000_000_000)
        assert payment is not None
        assert (payment.interest, payment.rental_interest) == (0, 0)
        actual.append(payment.total)
        principal -= payment.principal
        mortgage.record_payment(payment, principal_after=principal)
    assert actual == payments
    assert principal == 0
    assert not mortgage.observe(principal).active
    assert mortgage.payment(len(payments) + 1, principal, 0) is None


def test_rental_interest_rounds_after_monthly_interest_and_uses_payment_month_fraction() -> None:
    mortgage = Mortgage(_terms(principal=150, rate=120_000_000, months=12))
    payment = mortgage.payment(1, 150, 250_000_000)
    assert payment is not None
    assert payment.interest == 2  # 1.5 cents, half-up at the monthly interest boundary.
    assert payment.rental_interest == 1  # A quarter of the recorded 2 cents, half-up.
    principal = 150 - payment.principal
    mortgage.record_payment(payment, principal_after=principal)
    owner_only = mortgage.payment(2, principal, 0)
    assert owner_only is not None
    assert owner_only.rental_interest == 0
    mortgage.record_payment(owner_only, principal_after=principal - owner_only.principal)
    assert mortgage.rental_interest_paid_ytd == 1
    assert mortgage.interest_paid_ytd == payment.interest + owner_only.interest


def test_payoff_preserves_year_to_date_facts_until_successful_assessment() -> None:
    mortgage = Mortgage(_terms())
    payment = mortgage.payment(1, 40_000_000, 250_000_000)
    assert payment is not None
    mortgage.record_payment(payment, principal_after=39_960_180)
    mortgage.payoff()
    before_assessment = mortgage.observe(0)
    assert not before_assessment.active
    assert (before_assessment.interest_paid_ytd, before_assessment.rental_interest_paid_ytd) == (200_000, 50_000)
    assert mortgage.payment(2, 0, 0) is None
    mortgage.reset_year()
    assert mortgage.observe(0) == before_assessment.model_copy(
        update={"interest_paid_ytd": 0, "rental_interest_paid_ytd": 0}
    )


def test_rejected_or_mismatched_settlement_does_not_mutate_servicing_facts() -> None:
    mortgage = Mortgage(_terms())
    opening = mortgage.observe(40_000_000)
    payment = mortgage.payment(1, 40_000_000, 0)
    assert payment is not None
    # A cash rejection means there is no record_payment call, and a bad ledger
    # confirmation fails before modifying any paid-interest or active state.
    assert mortgage.observe(40_000_000) == opening
    with pytest.raises(ValueError, match="settled principal"):
        mortgage.record_payment(payment, principal_after=40_000_000)
    assert mortgage.observe(40_000_000) == opening
    foreign = replace(payment, terms=replace(payment.terms, liability_id="different-loan"))
    with pytest.raises(ValueError, match="does not belong"):
        mortgage.record_payment(foreign, principal_after=39_960_180)
    assert mortgage.observe(40_000_000) == opening


@pytest.mark.parametrize(("principal", "rate", "months"), [(0, 0, 1), (1, -1, 1), (1, 1_000_000_001, 1), (1, 0, 0)])
def test_invalid_terms_fail_before_servicing(principal: int, rate: int, months: int) -> None:
    with pytest.raises(ValueError, match=r"positive|nonnegative|not exceed"):
        Mortgage(_terms(principal=principal, rate=rate, months=months))


if __name__ == "__main__":
    pytest_bazel.main()
