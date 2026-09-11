"""Balanced journal and atomic rejection, including compound postings and overflow."""

import pytest
import pytest_bazel

from finance.augur.sim.books import AccountRef, JournalEntry, Posting
from finance.augur.sim.ledger import Ledger
from finance.augur.sim.money import MAX_COUNT

CASH = AccountRef(agent_id="test_household", account_id="cash")
BASIS = AccountRef(agent_id="test_household", account_id="asset_basis:test_fund")
GAIN = AccountRef(agent_id="test_household", account_id="income:realized_gain")


@pytest.fixture
def ledger() -> Ledger:
    return Ledger([CASH, BASIS, GAIN])


def test_compound_entry_balances_and_applies_atomically(ledger: Ledger) -> None:
    ledger.apply(
        JournalEntry(
            month=4,
            cause_id="sale",
            postings=[
                Posting(account=CASH, amount=150),
                Posting(account=BASIS, amount=-100),
                Posting(account=GAIN, amount=-50),
            ],
        )
    )
    assert ledger.balance(CASH) == 150
    assert ledger.balance(BASIS) == -100
    assert ledger.balance(GAIN) == -50
    assert ledger.trial_balance() == 0


def test_rejects_unbalanced_entry_without_mutation(ledger: Ledger) -> None:
    before = dict(ledger.balances)
    with pytest.raises(ValueError, match="unbalanced by 1 quanta"):
        ledger.apply(JournalEntry(month=0, cause_id="bad", postings=[Posting(account=CASH, amount=1)]))
    assert ledger.balances == before


def test_repeated_account_postings_are_accumulated_before_mutation(ledger: Ledger) -> None:
    ledger.apply(
        JournalEntry(
            month=0,
            cause_id="compound",
            postings=[
                Posting(account=CASH, amount=100),
                Posting(account=CASH, amount=50),
                Posting(account=BASIS, amount=-150),
            ],
        )
    )
    assert ledger.balance(CASH) == 150
    assert ledger.balance(BASIS) == -150
    assert ledger.trial_balance() == 0


def test_unknown_account_does_not_commit_a_successful_prefix(ledger: Ledger) -> None:
    before = dict(ledger.balances)
    with pytest.raises(KeyError):
        ledger.apply(
            JournalEntry(
                month=0,
                cause_id="unknown",
                postings=[
                    Posting(account=CASH, amount=1),
                    Posting(account=AccountRef(agent_id="test_missing", account_id="cash"), amount=-1),
                ],
            )
        )
    assert ledger.balances == before


def test_overflow_does_not_commit_a_successful_prefix(ledger: Ledger) -> None:
    ledger.apply(
        JournalEntry(
            month=0,
            cause_id="opening",
            postings=[Posting(account=CASH, amount=MAX_COUNT), Posting(account=BASIS, amount=-MAX_COUNT)],
        )
    )
    before = dict(ledger.balances)
    with pytest.raises(OverflowError):
        ledger.apply(
            JournalEntry(
                month=1,
                cause_id="overflow",
                postings=[Posting(account=GAIN, amount=-1), Posting(account=CASH, amount=1)],
            )
        )
    assert ledger.balances == before


def test_compound_delta_overflow_does_not_cancel_silently(ledger: Ledger) -> None:
    before = dict(ledger.balances)
    with pytest.raises(OverflowError):
        ledger.apply(
            JournalEntry(
                month=0,
                cause_id="compound_overflow",
                postings=[
                    Posting(account=CASH, amount=MAX_COUNT),
                    Posting(account=CASH, amount=1),
                    Posting(account=CASH, amount=-1),
                    Posting(account=BASIS, amount=-MAX_COUNT),
                ],
            )
        )
    assert ledger.balances == before


if __name__ == "__main__":
    pytest_bazel.main()
