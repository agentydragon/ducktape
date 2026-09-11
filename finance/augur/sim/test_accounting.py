"""Native cash admission controls, preserving full books and successful action prefixes."""

from copy import deepcopy

import pytest
import pytest_bazel

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.actions import Transfer
from finance.augur.sim.books import AccountRef, JournalEntry, Posting
from finance.augur.sim.money import MAX_COUNT, MIN_COUNT
from finance.augur.sim.scenario import ORDINARY_INCOME, TransferDeductionCategory, TransferIncomeCategory
from finance.augur.sim.testing.accounting import CASH, EXOGENOUS, HOUSEHOLD, RECIPIENT, RESERVE, accounting


@pytest.fixture
def books() -> Accounting:
    return accounting()


@pytest.fixture
def transfer_request() -> Transfer:
    return Transfer(cause_id="move-cash", from_account=CASH, to_account=RECIPIENT, amount=25)


def snapshot(books: Accounting) -> tuple[object, ...]:
    return (
        dict(books.ledger.balances),
        deepcopy(books.tax.income.by_source),
        deepcopy(books.tax.years),
        list(books.journal),
        books.journal_entry_count,
        list(books.transfers),
    )


def test_admitted_actor_transfer_matches_scheduled_accounting_exactly(transfer_request: Transfer) -> None:
    actor, scheduled = accounting(), accounting()
    actor.transfer(0, transfer_request, actor=HOUSEHOLD)
    scheduled.transfer(0, transfer_request, actor=None)
    assert snapshot(actor) == snapshot(scheduled)
    assert actor.ledger.balance(CASH) == 75
    assert actor.ledger.balance(RECIPIENT) == 25
    assert actor.transfers[0].cause_id == "move-cash"
    assert actor.transfers[0].amount == 25
    assert actor.ledger.trial_balance() == 0


def test_scheduled_income_can_arrive_from_an_exogenous_negative_balance(
    books: Accounting, transfer_request: Transfer
) -> None:
    books.transfer(
        0, transfer_request.model_copy(update={"from_account": EXOGENOUS}), actor=None, income=ORDINARY_INCOME
    )
    assert books.ledger.balance(EXOGENOUS) == -25
    assert books.tax.income.ordinary(RECIPIENT.agent_id) == 25
    assert books.transfers[0].income_category == "ordinary"


@pytest.mark.parametrize("case", range(8))
def test_actors_cannot_overdraw_or_impersonate_another_source_or_classify_tax(
    books: Accounting, transfer_request: Transfer, case: int
) -> None:
    changes: list[dict[str, object]] = [
        {"amount": 101},
        {"amount": 0},
        {"amount": -1},
        {"from_account": EXOGENOUS},
        {"from_account": AccountRef(agent_id=HOUSEHOLD, account_id="missing")},
        {"to_account": AccountRef(agent_id=RECIPIENT.agent_id, account_id="missing")},
        {"cause_id": ""},
        {},
    ]
    before = snapshot(books)
    income: TransferIncomeCategory | None = ORDINARY_INCOME if case == 7 else None
    with pytest.raises(ValueError, match=r"amount must|source account|unknown declared|empty transfer|bare actor"):
        books.transfer(0, transfer_request.model_copy(update=changes[case]), actor=HOUSEHOLD, income=income)
    assert snapshot(books) == before


@pytest.mark.parametrize(
    ("case", "deduction"), [(0, "ordinary"), (1, "ordinary"), (2, "ordinary"), (3, "ordinary"), (4, "invalid")]
)
def test_scheduled_tax_and_posting_failures_do_not_partially_apply(
    books: Accounting, transfer_request: Transfer, case: int, deduction: TransferDeductionCategory
) -> None:
    if case == 0:
        books.tax.income.accrue(RECIPIENT.agent_id, ORDINARY_INCOME, MAX_COUNT)
    elif case == 1:
        books.tax.income.accrue(HOUSEHOLD, ORDINARY_INCOME, MIN_COUNT)
    elif case == 2:
        books.journal_entry_count = (1 << 64) - 1
    elif case == 3:
        books.ledger.apply(
            JournalEntry(
                month=0,
                cause_id="large",
                postings=[Posting(account=RECIPIENT, amount=MAX_COUNT), Posting(account=EXOGENOUS, amount=-MAX_COUNT)],
            )
        )
    before = snapshot(books)
    if case == 4:
        assert_invalid_deduction(books, transfer_request, deduction)
    else:
        with pytest.raises(OverflowError):
            books.transfer(0, transfer_request, actor=None, income=ORDINARY_INCOME, deduction="ordinary")
    assert snapshot(books) == before


def assert_invalid_deduction(
    books: Accounting, transfer_request: Transfer, deduction: TransferDeductionCategory
) -> None:
    with pytest.raises(ValueError, match="unsupported deduction"):
        books.transfer(0, transfer_request, actor=None, income=ORDINARY_INCOME, deduction=deduction)


def test_shared_income_row_is_updated_in_order_without_overwriting_a_prior_change(
    books: Accounting, transfer_request: Transfer
) -> None:
    books.transfer(
        0,
        transfer_request.model_copy(update={"to_account": RESERVE}),
        actor=None,
        income=ORDINARY_INCOME,
        deduction="ordinary",
    )
    assert books.tax.income.ordinary(HOUSEHOLD) == 0
    assert books.ledger.balance(CASH) == 75
    assert books.ledger.balance(RESERVE) == 25
    assert books.ledger.trial_balance() == 0


def test_transfer_sequence_is_not_an_implicitly_atomic_batch(books: Accounting, transfer_request: Transfer) -> None:
    books.transfer(0, transfer_request, actor=HOUSEHOLD)
    before = snapshot(books)
    with pytest.raises(ValueError, match="covered by available cash"):
        books.transfer(0, transfer_request.model_copy(update={"amount": 76}), actor=HOUSEHOLD)
    assert snapshot(books) == before
    assert len(books.transfers) == 1


def test_zero_scheduled_cashflow_still_has_a_balanced_journal_entry(
    books: Accounting, transfer_request: Transfer
) -> None:
    count = books.journal_entry_count
    books.transfer(0, transfer_request.model_copy(update={"amount": 0}), actor=None)
    assert books.journal_entry_count == count + 1
    assert books.transfers[-1].amount == 0
    assert books.ledger.trial_balance() == 0


if __name__ == "__main__":
    pytest_bazel.main()
