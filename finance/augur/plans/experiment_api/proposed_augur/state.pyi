"""Opening financial facts, independent of a policy or web scenario.

Actual books retain lots, basis, known contracts and filing/payment state.
Synthetic NoTax index studies may construct a clean opening allocation. Policy
memory belongs to the experiment, not a portable full-executor checkpoint.
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
        cls, *, actor: Actor, capital: Money, weights: Weights, taxes: NoTax, calendar: Calendar, basis: ReportingBasis
    ) -> Situation:
        """Synthetic clean book in AccountRef(actor, "portfolio") for cash and lots.

        Cannot replace an actual taxed portfolio or re-establish a continuing book.
        """
    def actor(self, name: str) -> Actor: ...
    def with_actors(self, *actors: Actor) -> Situation: ...
