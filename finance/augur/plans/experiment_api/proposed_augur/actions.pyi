"""Actor requests, executed in supplied order through canonical accounting.

A batch is not atomic: a rejected action changes no state, preserves the successful
prefix and stops this rollout. No retry or hidden funding/rebalancing pass.
Housing actions accept supplied terms; they cannot assert lender approval.
"""

from dataclasses import dataclass
from decimal import Decimal
from proposed_augur.accounting import AccountRef, LotQuantity
from proposed_augur.contracts import ClaimId, LeaseOffer, PurchaseOffer
from proposed_augur.instruments import Instrument
from proposed_augur.money import Money

@dataclass(frozen=True)
class Sell:
    account: AccountRef
    lots: tuple[LotQuantity, ...]

@dataclass(frozen=True)
class Buy:
    account: AccountRef
    instrument: Instrument
    units: Decimal

@dataclass(frozen=True)
class Transfer:
    source: AccountRef
    destination: AccountRef
    amount: Money

@dataclass(frozen=True)
class PayClaim:
    source: AccountRef
    claim: ClaimId
    amount: Money

@dataclass(frozen=True)
class Consume:
    source: AccountRef
    component: str
    amount: Money

@dataclass(frozen=True)
class AcceptLease:
    source: AccountRef
    offer: LeaseOffer

@dataclass(frozen=True)
class PurchaseHome:
    source: AccountRef
    offer: PurchaseOffer

type Action = Sell | Buy | Transfer | PayClaim | Consume | AcceptLease | PurchaseHome
