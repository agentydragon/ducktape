"""Read-only actor books and lot identities; execution owns ledger mutations.

Policies select lots; they cannot apply events or edit a book.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from proposed_augur.instruments import Instrument
from proposed_augur.money import Money

@dataclass(frozen=True)
class Actor:
    name: str
    @classmethod
    def external(cls, name: str) -> Actor: ...

@dataclass(frozen=True)
class AccountRef:
    actor: Actor
    name: str

@dataclass(frozen=True)
class LotQuantity:
    lot_id: str
    units: Decimal

@dataclass(frozen=True)
class TaxLot:
    lot_id: str
    account: AccountRef
    instrument: Instrument
    units: Decimal
    acquired: date
    basis: Money

class Book:
    def lots(self, instrument: Instrument) -> tuple[TaxLot, ...]: ...
    def available_cash(self, account: AccountRef) -> Money: ...

class FinancialEvent:
    """Actual execution/payment facts, carrying originating request identity."""

    at: date
    actor: Actor
    request_id: str
    amounts: Mapping[str, Money]
