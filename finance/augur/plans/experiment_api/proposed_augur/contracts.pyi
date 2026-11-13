"""Known contracts, due claims and counterparty-supplied offers.

Claims are information, not automatic funding instructions. Execution creates
contracts only after validating the actor's acceptance against offered terms.
Offer eligibility, payment deadlines and settlement timing remain explicit.
"""

from dataclasses import dataclass
from datetime import date
from proposed_augur.accounting import Actor
from proposed_augur.instruments import Home
from proposed_augur.money import Money

type ClaimId = str

@dataclass(frozen=True)
class Claim:
    id: ClaimId
    payer: Actor
    payee: Actor
    due: date
    amount: Money

class Contract: ...

class MortgageOffer:
    """Lender-supplied eligibility and financing terms, not a policy request."""

    name: str

class LeaseOffer:
    home: Home
    landlord: Actor

class PurchaseOffer:
    """Seller terms and, when used, the lender's mortgage offer."""

    home: Home
    seller: Actor
    mortgage: MortgageOffer | None

class Lease(Contract): ...
class PropertyPurchase(Contract): ...
class FixedRateMortgage(Contract): ...
