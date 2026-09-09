"""Executable decision rules and convenient reusable study/household policies.

Maps visible observations and path-local memory to spending, trading and action
proposals. Does not mutate books or reproduce tax/mortgage arithmetic. Strategy
composes separable rules; from_review permits coordinated decisions with an
author-defined state type. Built-in study rules are conveniences, not enum variants
the engine must learn. Parameters are data; behavior remains replaceable code.

The factories below show library reuse, not a requirement that all of these live
in one implementation file. Batched arrays are illustrative, not a commitment to
NumPy, callback compilation, or a particular Python/Rust boundary.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from proposed_augur.accounting import Actor
from proposed_augur.contracts import FixedRateMortgage, Lease, PropertyPurchase
from proposed_augur.instruments import ExecutionCosts, Home, Instrument, Weights
from proposed_augur.markets import ForecastOrigins, RandomStreams
from proposed_augur.money import BoolArray, Money, PriceIndex, RealAmount, RealBatch, ReportingBasis
from proposed_augur.results import Events
from proposed_augur.state import CheckpointBatch

class Observations:
    """Read-only, path-keyed information available at this decision point, never the sampled future."""

    actor: Actor
    basis: ReportingBasis
    path_ids: tuple[str, ...]
    receipts: Events
    wealth_real: RealBatch
    liquid_wealth_real: RealBatch
    annual_budget_real: RealBatch
    review_index: NDArray[np.int64]
    next_period_index: NDArray[np.int64]
    move_pending: BoolArray
    def at_destination(self, destination: str) -> BoolArray: ...
    def checkpoints(self) -> CheckpointBatch: ...
    def forecast_origins(self) -> ForecastOrigins: ...
    def random_stream(self, purpose: str) -> RandomStreams: ...

@dataclass(frozen=True)
class BudgetDecision:
    """Requested real annual budget and decision memory; neither certifies actual payment."""

    budget: RealBatch
    state: RealBatch

type BudgetReview = Callable[[Observations, RealBatch], BudgetDecision]

type TargetReview = Callable[[Observations], Weights]

class SpendingPolicy: ...

class InvestmentPolicy:
    name: str

class ActionPolicy: ...
class ActionState: ...
class ActionBatch: ...

@dataclass(frozen=True)
class ActionDecision:
    actions: ActionBatch
    state: ActionState

type ActionReview = Callable[[Observations, ActionState], ActionDecision]

class AnnualSpending(SpendingPolicy):
    def __init__(
        self, *, review: BudgetReview, initial_state: RealAmount, consume_every: Literal["month", "year"] = "year"
    ) -> None: ...

class FixedWithdrawal(SpendingPolicy):
    def __init__(self, *, initial: Money, index: PriceIndex | None, interval: Literal["month", "year"]) -> None: ...
    @classmethod
    def from_real(
        cls, amount: RealBatch, *, interval: Literal["month"], budget_period: Literal["year"]
    ) -> FixedWithdrawal: ...

class AnnualRebalance(InvestmentPolicy):
    def __init__(self, *, target: Weights | TargetReview, transaction_cost: float) -> None: ...

class CashReserve: ...
class DriftBand: ...

class Allocation(InvestmentPolicy):
    def __init__(
        self,
        *,
        target: Weights,
        reserve: CashReserve,
        rebalance: DriftBand,
        establish_target: Literal["trade_from_opening_book"],
        reinvest_surplus: bool,
        lot_selection: Literal["fifo"],
        transaction_costs: ExecutionCosts,
    ) -> None: ...

class SpendingAnchor:
    name: str

class LifestylePlan:
    def policy(
        self,
        *,
        initial_anchor: SpendingAnchor,
        flex: BudgetReview,
        transitions: ActionReview,
        review_every: Literal["year"],
        consume_every: Literal["month"],
    ) -> SpendingPolicy: ...

class MoveTerms:
    destination: str

class Move:
    @staticmethod
    def request(*, terms: MoveTerms, where: BoolArray, accepted_tag: str) -> ActionBatch: ...

class HousingDecision:
    @staticmethod
    def lease(lease: Lease, *, tenant: Actor, landlord: Actor) -> ActionPolicy: ...
    @staticmethod
    def buy(
        purchase: PropertyPurchase,
        *,
        home: Home,
        buyer: Actor,
        seller: Actor,
        financing: FixedRateMortgage,
        disposition: Literal["retain_at_horizon"],
        accepted_tag: str,
    ) -> ActionPolicy: ...

class Strategy:
    def __init__(
        self, *, spending: SpendingPolicy, trading: InvestmentPolicy, actions: Sequence[ActionPolicy] = ()
    ) -> None: ...
    @classmethod
    def from_review[State](
        cls,
        *,
        review: Callable[[Observations, State], PolicyStep[State]],
        initial_state: State,
        review_every: Literal["month", "year"],
        trading: InvestmentPolicy,
    ) -> Strategy: ...

@dataclass(frozen=True)
class Proposal:
    """A coordinated request. None leaves that decision unchanged; execution returns actual receipts."""

    budget: RealBatch | None = None
    target: Weights | None = None
    actions: ActionBatch | None = None
    tags: tuple[str, ...] = ()

@dataclass(frozen=True)
class PolicyStep[State]:
    proposal: Proposal
    state: State

class GuytonPortfolioConvention: ...
class GuytonRuleOrder: ...

def guyton_klinger_policy(
    *,
    target: Weights,
    equities: tuple[Instrument, ...],
    fixed_income: tuple[Instrument, ...],
    reserve: Instrument,
    initial_withdrawal: Money,
    price_index: PriceIndex,
    freeze: Literal["negative_return_and_rate_above_initial"],
    inflation_cap: float | None,
    preserve_above_initial_ratio: float,
    preservation_cut: float,
    preservation_inactive_final_years: int,
    prosper_below_initial_ratio: float | None,
    prosperity_raise: float,
    rule_order: GuytonRuleOrder,
    portfolio_convention: GuytonPortfolioConvention,
    transaction_cost: float,
) -> Strategy: ...
