"""Ownership, books, lots and balanced financial events across actors.

Records what was held, owed and transacted; preserves cost basis and counterparties.
Does not choose trades or implement tax statutes. Taxes consumes these facts;
simulation settles decisions against these books. External actors can balance
transfers without an optimizing policy or invented personal tax profile.
"""

from collections.abc import Mapping, Sequence
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
class TaxLot:
    instrument: Instrument
    units: Decimal
    acquired: date
    basis: Money

class Book:
    """One actor's cash, holdings/lots, receivables and liabilities."""

    def lots(self, instrument: Instrument) -> Sequence[TaxLot]: ...
    def cash(self, currency: str) -> Money: ...

class FinancialEvent:
    """Dated transaction facts, including counterparties and affected lots.

    Carries proceeds, costs, income/distribution character and other facts needed
    by supported tax rules. Detailed event variants are not specified in this pass.
    It records an executed event, not a desired allocation or an inferred tax bill.
    """

class Ledger:
    def books(self) -> Mapping[Actor, Book]: ...
    def apply(self, events: Sequence[FinancialEvent]) -> Ledger:
        """Return updated books, checking balance and lot consistency across actors."""
