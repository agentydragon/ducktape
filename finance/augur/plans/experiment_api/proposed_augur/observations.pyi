"""Read-only, actor-known information at one monthly decision.

No remaining sampled path, other actors' private books or mutable executor.
Receipts describe successful actions; routing identities live outside this view.
Valuation uses declared information and reporting assumptions.
"""

from dataclasses import dataclass
from datetime import date
from proposed_augur.accounting import AccountRef, Actor, Book, FinancialEvent
from proposed_augur.contracts import Claim, Contract
from proposed_augur.data import NamedSeries
from proposed_augur.money import Money, RealAmount, ReportingBasis
from proposed_augur.taxes import TaxRecords

@dataclass(frozen=True)
class Observation:
    actor: Actor
    at: date
    month_index: int
    months_remaining: int
    basis: ReportingBasis
    book: Book
    contracts: tuple[Contract, ...]
    due_claims: tuple[Claim, ...]
    market: NamedSeries
    tax_records: TaxRecords
    events: tuple[FinancialEvent, ...]
    wealth_real: RealAmount
    liquid_wealth_real: RealAmount
    def nominal(self, amount: RealAmount) -> Money: ...
    def real(self, amount: Money) -> RealAmount: ...
    def available_cash(self, account: AccountRef) -> Money: ...
