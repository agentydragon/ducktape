"""Canonical signed-debit balances. Journal rejection leaves every account unchanged."""

from collections.abc import Iterable, Mapping, Sequence
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
        self.apply_all([entry])

    def apply_all(self, entries: Sequence[JournalEntry]) -> None:
        """Entries apply in order and commit as one group; staging holds only the posted accounts.

        A rejection anywhere — unbalanced entry, unknown account, overflow — leaves every account
        unchanged.
        """
        staged: dict[AccountRef, int] = {}
        for entry in entries:
            imbalance = sum(posting.amount for posting in entry.postings)
            if imbalance:
                raise ValueError(
                    f"journal entry {entry.cause_id!r} at month {entry.month} is unbalanced by {imbalance} quanta"
                )
            deltas: dict[AccountRef, int] = {}
            for posting in entry.postings:
                checked_count(posting.amount, "posting amount")
                deltas[posting.account] = checked_count(
                    deltas.get(posting.account, 0) + posting.amount, "money addition"
                )
            for account, delta in deltas.items():
                current = staged[account] if account in staged else self.balance(account)
                staged[account] = checked_count(current + delta, "money addition")
        self._balances.update(staged)

    def trial_balance(self) -> int:
        return sum(self._balances.values())
