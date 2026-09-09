"""Optional Python-friendly action calculators, never executors.

Authors can replace these algorithms. Each returns explicit ordered actions;
execution does not apply another allocator afterward. Account bindings, lot order,
cash retained, costs and settlement availability are explicit inputs.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from proposed_augur.accounting import AccountRef
from proposed_augur.actions import Action
from proposed_augur.instruments import Instrument, Weights
from proposed_augur.money import Money
from proposed_augur.observations import Observation

@dataclass(frozen=True)
class Portfolio:
    """Declared account bindings, not targets or an execution strategy."""

    cash: AccountRef
    holdings: Mapping[Instrument, AccountRef]

@dataclass(frozen=True)
class PreviewAssumptions:
    """Declared timing/tax assumptions; no hidden future assessment.

    The first control uses immediate cash. Product terms are a destination
    interface, not a claim that delayed settlement is already supported.
    """

    settlement: Literal["immediate", "product_terms"]

class PreviewError(Exception): ...

def preview(observation: Observation, actions: tuple[Action, ...], *, assumptions: PreviewAssumptions) -> Observation:
    """Non-mutating canonical calculations over known inputs, not a callback.

    Raises PreviewError for an unexecutable prefix under supplied assumptions.
    Does not know future settlement, market or tax outcomes.
    """

def raise_cash(
    observation: Observation,
    portfolio: Portfolio,
    *,
    required_cash: Money,
    lot_order: Literal["fifo"],
    assumptions: PreviewAssumptions,
) -> tuple[Action, ...]:
    """Propose sells/transfers, no spending change or invented borrowing.

    Required cash includes available cash, not an additional sale amount. If
    holdings are insufficient, propose available funding only; the caller's
    unchanged payment/consumption action will fail and stop the path.
    """

def rebalance(
    observation: Observation,
    portfolio: Portfolio,
    *,
    targets: Weights,
    retain_cash: Money,
    lot_order: Literal["fifo"],
    assumptions: PreviewAssumptions,
) -> tuple[Action, ...]:
    """Propose ordered exact-lot trades within available cash and declared costs.

    Zero target means full exit: a destination requirement, not a claim about
    today's native interior-allocation arithmetic.
    """
