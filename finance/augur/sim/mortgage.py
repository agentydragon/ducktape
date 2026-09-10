"""Fixed-rate mortgage servicing over a principal balance owned by the ledger.

Quotes are read-only. Record a payment or payoff only after its ledger entries
settle; retain paid-interest facts until the caller completes tax assessment.
"""

from dataclasses import dataclass, field

from finance.augur.sim.books import AccountRef, MortgageState
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE

_CONTRACT_SCALE = 10**18


def _count(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer count")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    if value >= 1 << 63:
        raise OverflowError(f"{name} does not fit signed 64-bit counts")
    return value


def _round_ratio(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


@dataclass(frozen=True, kw_only=True)
class MortgageTerms:
    """Fixed contract facts, including the amount borrowed rather than today's balance."""

    liability_id: str
    property_id: str
    borrower: AccountRef
    lender: AccountRef
    origination_month: int
    origination_principal: int
    annual_interest_rate_ppb: int
    term_months: int

    def __post_init__(self) -> None:
        identifiers = (
            self.liability_id,
            self.property_id,
            self.borrower.agent_id,
            self.borrower.account_id,
            self.lender.agent_id,
            self.lender.account_id,
        )
        if any(not identifier.strip() for identifier in identifiers):
            raise ValueError("mortgage identities must not be empty")
        _count(self.origination_month, "origination month")
        _count(self.term_months, "term months")
        _count(self.origination_principal, "origination principal")
        _count(self.annual_interest_rate_ppb, "annual interest rate")
        if self.origination_month >= 1 << 32 or not 0 < self.term_months < 1 << 32:
            raise ValueError("mortgage months must fit unsigned 32-bit counts and term must be positive")
        if not self.origination_principal or self.annual_interest_rate_ppb > MONEY_FACTOR_SCALE:
            raise ValueError("mortgage principal must be positive and annual interest rate must not exceed one")


def _monthly_payment(terms: MortgageTerms) -> int:
    if not terms.annual_interest_rate_ppb:
        return _round_ratio(terms.origination_principal, terms.term_months)
    rate = _round_ratio(terms.annual_interest_rate_ppb * _CONTRACT_SCALE, 12 * MONEY_FACTOR_SCALE)
    discount = _CONTRACT_SCALE
    for _ in range(terms.term_months):
        discount = _round_ratio(discount * _CONTRACT_SCALE, _CONTRACT_SCALE + rate)
    return _count(_round_ratio(terms.origination_principal * rate, _CONTRACT_SCALE - discount), "monthly payment")


@dataclass(frozen=True, kw_only=True)
class MortgagePayment:
    """An installment quote; principal is the paid portion, not the loan balance.

    principal_before binds this quote to the supplied ledger balance so recording
    settlement can check its result without storing another outstanding balance.
    """

    terms: MortgageTerms
    month: int
    principal_before: int
    interest: int
    principal: int
    total: int
    rental_interest: int


@dataclass
class Mortgage:
    """Servicing memory; each operation reads outstanding principal from its caller's ledger."""

    terms: MortgageTerms
    monthly_payment: int = field(init=False)
    interest_paid_ytd: int = field(init=False, default=0)
    rental_interest_paid_ytd: int = field(init=False, default=0)
    active: bool = field(init=False, default=True)

    def __post_init__(self) -> None:
        self.monthly_payment = _monthly_payment(self.terms)

    def payment(self, month: int, principal: int, rented_fraction_ppb: int) -> MortgagePayment | None:
        """Quote the next installment without changing servicing or ledger state."""
        _count(month, "payment month")
        _count(principal, "principal")
        _count(rented_fraction_ppb, "rented fraction")
        if rented_fraction_ppb > MONEY_FACTOR_SCALE:
            raise ValueError("rented fraction must not exceed one")
        if not self.active or month <= self.terms.origination_month or not principal:
            return None
        interest = _count(
            _round_ratio(principal * self.terms.annual_interest_rate_ppb, 12 * MONEY_FACTOR_SCALE), "interest"
        )
        total = min(self.monthly_payment, _count(principal + interest, "principal plus interest"))
        principal_paid = min(principal, max(0, total - interest))
        return MortgagePayment(
            terms=self.terms,
            month=month,
            principal_before=principal,
            interest=interest,
            principal=principal_paid,
            total=total,
            rental_interest=_round_ratio(interest * rented_fraction_ppb, MONEY_FACTOR_SCALE),
        )

    def record_payment(self, payment: MortgagePayment, principal_after: int) -> None:
        """Commit paid-interest facts after the quoted installment settles in full."""
        _count(principal_after, "remaining principal")
        if payment.terms != self.terms or not self.active:
            raise ValueError("payment does not belong to this active mortgage")
        if principal_after != payment.principal_before - payment.principal:
            raise ValueError("settled principal does not match the mortgage payment quote")
        interest = _count(self.interest_paid_ytd + payment.interest, "year-to-date interest")
        rental = _count(self.rental_interest_paid_ytd + payment.rental_interest, "year-to-date rental interest")
        self.interest_paid_ytd = interest
        self.rental_interest_paid_ytd = rental
        self.active = principal_after > 0

    def payoff(self) -> None:
        """Close after successful ledger payoff; paid-interest tax facts survive."""
        self.active = False

    def reset_year(self) -> None:
        """Clear interest totals only after the caller's tax assessment succeeds."""
        self.interest_paid_ytd = 0
        self.rental_interest_paid_ytd = 0

    def observe(self, principal: int) -> MortgageState:
        return MortgageState(
            liability_id=self.terms.liability_id,
            property_id=self.terms.property_id,
            agent_id=self.terms.borrower.agent_id,
            payment_account_id=self.terms.borrower.account_id,
            counterparty_agent_id=self.terms.lender.agent_id,
            counterparty_account_id=self.terms.lender.account_id,
            origination_month=self.terms.origination_month,
            annual_interest_rate_ppb=self.terms.annual_interest_rate_ppb,
            term_months=self.terms.term_months,
            monthly_payment=self.monthly_payment,
            principal=_count(principal, "observed principal"),
            interest_paid_ytd=self.interest_paid_ytd,
            rental_interest_paid_ytd=self.rental_interest_paid_ytd,
            active=self.active,
        )
