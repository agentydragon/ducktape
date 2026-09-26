"""Occurrence-scoped payment admission."""

from copy import deepcopy
from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim import results
from finance.augur.sim.accounting import Accounting
from finance.augur.sim.actions import ClaimId, Consume, PayClaim
from finance.augur.sim.actor import MonthOpened
from finance.augur.sim.books import AccountRef
from finance.augur.sim.claims import Claim, Claims
from finance.augur.sim.ids import AccountId, AgentId
from finance.augur.sim.payments import execute
from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.testing.accounting import (
    CASH,
    EXOGENOUS,
    HOUSEHOLD,
    RECIPIENT,
    RESERVE,
    accounting,
    opening,
    taxpayer,
)


@pytest.fixture
def books() -> Accounting:
    return accounting()


@pytest.fixture
def claims() -> Claims:
    return Claims(
        0,
        [
            Claim("same-label_m0", "rent", CASH, RECIPIENT, 30, None),
            Claim("same-label_m0", "rent", CASH, RECIPIENT, 40, None),
        ],
    )


def state(books: Accounting, claims: Claims) -> tuple[object, ...]:
    return (
        dict(books.ledger.balances),
        deepcopy(books.tax.income.by_source),
        deepcopy(books.tax.years),
        list(books.journal),
        list(books.transfers),
        deepcopy(claims.entries),
    )


def test_claim_occurrences_are_not_labels_and_consumption_is_not_a_claim(books: Accounting, claims: Claims) -> None:
    ids = [id_ for id_, _ in claims.due(HOUSEHOLD)]
    assert ids[0] != ids[1]
    request = PayClaim(request_id=42, cause_id="chosen-payment", claim=ids[1], from_account=CASH, amount=40)
    receipt = execute(books, 0, claims, HOUSEHOLD, request)
    assert receipt.request_id == 42
    assert receipt.target == results.ClaimTarget(month=0, index=1)
    assert receipt.amount_paid == 40
    assert [id_ for id_, _ in claims.due(HOUSEHOLD)] == [ids[0]]
    before = state(books, claims)
    assert execute(books, 0, claims, HOUSEHOLD, request).outcome == results.PaymentRejected(
        reason=results.PaymentRequestError(kind="AlreadyPaid")
    )
    assert state(books, claims) == before
    consumption = Consume(
        request_id=43,
        cause_id="same-label_m0",
        component_id="flex-budget",
        from_account=CASH,
        to_account=RECIPIENT,
        amount=10,
    )
    receipt = execute(books, 0, claims, HOUSEHOLD, consumption)
    assert receipt.target == results.ConsumptionTarget(component_id="flex-budget")
    assert receipt.amount_paid == 10
    assert len(claims.entries) == 2
    assert not claims.entries[0].paid
    assert books.ledger.balance(CASH) == 50
    assert len(books.transfers) == 2


@pytest.mark.parametrize(
    ("changes", "kind"),
    [
        ({"claim": ClaimId(month=1, index=0)}, "UnknownClaim"),
        ({"claim": ClaimId(month=0, index=1)}, "UnknownClaim"),
        ({"amount": 50}, "InvalidAmount"),
        ({"from_account": RECIPIENT}, "WrongActor"),
        ({"from_account": AccountRef(agent_id=HOUSEHOLD, account_id=AccountId("undeclared"))}, "UnknownAccount"),
        ({"cause_id": ""}, "EmptyIdentifier"),
        ({}, "InsufficientCash"),
    ],
)
def test_rejected_payments_change_neither_books_nor_capture(
    books: Accounting, changes: dict[str, object], kind: str
) -> None:
    claims = Claims(0, [Claim("large-claim", "rent", CASH, RECIPIENT, 150, None)])
    request = PayClaim(
        request_id=8, cause_id="pay", claim=ClaimId(month=0, index=0), from_account=CASH, amount=150
    ).model_copy(update=changes)
    before = state(books, claims)
    receipt = execute(books, 0, claims, HOUSEHOLD, request)
    assert isinstance(receipt.outcome, results.PaymentRejected)
    assert receipt.outcome.reason.kind == kind
    assert receipt.amount_paid == 0
    assert state(books, claims) == before


@pytest.mark.parametrize(
    ("destination", "amount", "component", "kind"),
    [
        (RECIPIENT, 0, "budget", "InvalidAmount"),
        (RECIPIENT, -1, "budget", "InvalidAmount"),
        (RECIPIENT, 101, "budget", "InsufficientCash"),
        (RECIPIENT, 1, "", "EmptyIdentifier"),
        (AccountRef(agent_id=AgentId("test_other"), account_id=AccountId("undeclared")), 1, "budget", "UnknownAccount"),
    ],
)
def test_consumption_admission_preserves_all_books(
    books: Accounting, claims: Claims, destination: AccountRef, amount: int, component: str, kind: str
) -> None:
    before = state(books, claims)
    request = Consume(
        request_id=9,
        cause_id="consume",
        component_id=component,
        from_account=CASH,
        to_account=destination,
        amount=amount,
    )
    receipt = execute(books, 0, claims, HOUSEHOLD, request)
    assert isinstance(receipt.outcome, results.PaymentRejected)
    assert receipt.outcome.reason.kind == kind
    assert state(books, claims) == before


def test_a_foreign_claim_is_not_authorized_by_a_household_source(books: Accounting) -> None:
    claims = Claims(0, [Claim("foreign", "rent", RECIPIENT, EXOGENOUS, 1, None)])
    before = state(books, claims)
    receipt = execute(
        books,
        0,
        claims,
        HOUSEHOLD,
        PayClaim(request_id=1, cause_id="foreign", claim=ClaimId(month=0, index=0), from_account=CASH, amount=1),
    )
    assert receipt.outcome == results.PaymentRejected(reason=results.PaymentRequestError(kind="WrongActor"))
    assert state(books, claims) == before


@pytest.mark.parametrize("destination", [CASH, RESERVE])
def test_moving_cash_within_the_actor_is_not_paid_consumption(
    books: Accounting, claims: Claims, destination: AccountRef
) -> None:
    before = state(books, claims)
    receipt = execute(
        books,
        0,
        claims,
        HOUSEHOLD,
        Consume(
            request_id=1,
            cause_id="own-account",
            component_id="budget",
            from_account=CASH,
            to_account=destination,
            amount=10,
        ),
    )
    assert receipt.outcome == results.PaymentRejected(reason=results.PaymentRequestError(kind="SameActorRecipient"))
    assert state(books, claims) == before


def test_estimates_and_true_up_settle_the_same_annual_liability() -> None:
    profile = replace(taxpayer(HOUSEHOLD), prior_year_tax=400)
    books = accounting(opening({CASH: 2000}), (profile,))
    authority = TaxAuthority(profile)

    def assessed(month: int) -> Claims:
        authority.handle(books.liability_statement(month))
        return Claims(
            month,
            [
                Claim(a.cause_id, a.obligation_type, a.from_account, a.to_account, a.amount, a.effect)
                for a in authority.handle(MonthOpened(month=month))
            ],
        )

    def pay_in_full(claims: Claims) -> None:
        for id_, claim in claims.due(HOUSEHOLD):
            request = PayClaim(
                request_id=id_.index + 1,
                cause_id=claim.cause_id,
                claim=id_,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            assert execute(books, claims.month, claims, HOUSEHOLD, request).outcome == results.Paid()

    for month in (3, 5, 8):
        pay_in_full(assessed(month))
    books.tax.income.accrue(HOUSEHOLD, ORDINARY_INCOME, 10_000)
    books.close_tax_year(11, [])
    assert [liability.amount_owed for liability in books.tax_liabilities] == [1000]
    claims = assessed(12)
    assert [claim.amount_due for claim in claims.entries] == [100, 600]
    pay_in_full(claims)
    assert [payment.amount_paid for payment in books.tax_payments] == [100, 100, 100, 100, 600]
    assert books.ledger.balance(CASH) == 1000
    assert books.tax_liabilities[0].amount_owed == 0
    assert books.tax_liabilities[0].active
    assert books.tax_settlements[0].amount == 1000
    assert books.ledger.balance(AccountRef(agent_id=HOUSEHOLD, account_id=AccountId("asset:tax-prepayments"))) == 0
    assert books.ledger.trial_balance() == 0


if __name__ == "__main__":
    pytest_bazel.main()
