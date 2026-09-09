"""Agreed obligations and their financial lifecycle, including housing finance.

Owns payment schedules, amortization and explicit termination/payoff terms.
Policies request origination or cancellation; they cannot erase existing promises.
Execution settles the resulting cashflows across the actors' books, with taxes
applied separately. A house-price forecast belongs to markets, not the mortgage.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from proposed_augur.accounting import Actor
from proposed_augur.data import NamedSeries
from proposed_augur.instruments import Home
from proposed_augur.money import Money

@dataclass(frozen=True)
class Obligation:
    payer: Actor
    payee: Actor
    due: date
    amount: Money

class Contract:
    def due(self, *, at: date, observations: NamedSeries) -> Sequence[Obligation]:
        """Payments required by existing terms and known indices, not discretionary policy requests."""

class MortgageOffer:
    name: str

class Lease:
    """Lease terms; the housing decision supplies counterparties when requesting origination."""
class PropertyPurchase: ...

class FixedRateMortgage(Contract):
    def __init__(self, *, offer: MortgageOffer, borrower: Actor, lender: Actor, collateral: Home) -> None: ...
