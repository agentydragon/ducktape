"""Canonical signed-debit balances. Journal rejection leaves every account unchanged."""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from finance.augur.sim.books import AccountRef, JournalEntry
from finance.augur.sim.money import checked_count


class Ledger:
    def __init__(self, accounts: Iterable[AccountRef]) -> None:
        self._balances = dict.fromkeys(accounts, 0)

    def ensure_account(self, account: AccountRef) -> None:
        self._balances.setdefault(account, 0)

    def balance(self, account: AccountRef) -> int:
        return self._balances[account]

    @property
    def balances(self) -> Mapping[AccountRef, int]:
        return MappingProxyType(self._balances)

    def apply(self, entry: JournalEntry) -> None:
        imbalance = sum(posting.amount for posting in entry.postings)
        if imbalance:
            raise ValueError(
                f"journal entry {entry.cause_id!r} at month {entry.month} is unbalanced by {imbalance} quanta"
            )
        deltas: dict[AccountRef, int] = {}
        for posting in entry.postings:
            checked_count(posting.amount, "posting amount")
            deltas[posting.account] = checked_count(deltas.get(posting.account, 0) + posting.amount, "money addition")
        updated = {
            account: checked_count(self.balance(account) + delta, "money addition") for account, delta in deltas.items()
        }
        self._balances.update(updated)

    def trial_balance(self) -> int:
        return sum(self._balances.values())
