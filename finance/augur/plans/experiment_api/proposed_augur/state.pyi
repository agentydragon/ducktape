"""Financial starting situations and complete continuation checkpoints.

Assembles books, contracts, tax state, calendar and reporting basis. A situation
contains financial facts, not a web-app scenario or a sweep of policies. A checkpoint
also preserves pending events, execution position and path-local policy memory.
It is not a new opening balance sheet; continuing must not repeat opening trades
or rebase the purchasing-power unit.
"""

from collections.abc import Mapping, Sequence
from datetime import date

from proposed_augur.accounting import Actor, Book
from proposed_augur.contracts import Contract
from proposed_augur.data import Calendar
from proposed_augur.instruments import Weights
from proposed_augur.money import Money, ReportingBasis
from proposed_augur.taxes import NoTax, TaxRules, TaxState

class Situation:
    as_of: date
    basis: ReportingBasis
    def __init__(
        self,
        *,
        books: Mapping[Actor, Book],
        contracts: Sequence[Contract],
        taxes: TaxRules,
        tax_state: TaxState,
        calendar: Calendar,
        basis: ReportingBasis,
    ) -> None: ...
    @classmethod
    def investor(
        cls,
        *,
        actor: Actor,
        capital: Money,
        weights: Weights,
        taxes: NoTax,
        calendar: Calendar,
        basis: ReportingBasis,
    ) -> Situation:
        """Synthetic clean opening book for index studies; actual holdings use the full constructor."""
    def actor(self, name: str) -> Actor: ...
    def with_actors(self, *actors: Actor) -> Situation: ...

class CheckpointBatch:
    basis: ReportingBasis
