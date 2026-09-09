# Personal planning: spending flexibility and allocation together

Proposed Python, not runnable. This is a new experiment, not a published-study
reproduction. Inputs are a dated actual balance sheet, tender proceeds already
received, real tax lots/basis, outstanding taxes, contracts, spending anchors and
transition terms. No values here describe an actual account or person.

Vary lifestyle budget, flexibility, optional backstop, equity share and bond
product together, then repeat under separately identified market models. Inspect
actual consumption, cuts, transitions, default and wealth; choosing one optimum
requires an explicit preference criterion.

## One monthly function coordinates the actions

```python
from collections.abc import Callable
from dataclasses import dataclass
from itertools import product

import polars as pl

from proposed_augur.actions import Action, PayClaim
from proposed_augur.data import NamedSeries
from proposed_augur.instruments import Instrument, Weights
from proposed_augur.markets import MarketModel
from proposed_augur.money import RealAmount
from proposed_augur.observations import Observation
from proposed_augur.policies import Initialize, PolicyKey, Response
from proposed_augur.proposals import (
    Portfolio, PreviewAssumptions, PreviewError, preview, raise_cash, rebalance,
)
from proposed_augur.results import Observer, Runs, StudyResult, financial_observers
from proposed_augur.simulation import run as run_paths
from proposed_augur.state import Situation
from study_helpers import funded_consumption

type BudgetRule = Callable[[Observation, RealAmount], RealAmount]
type Transition = Callable[[Observation, RealAmount], tuple[Action, ...]]


def household_policy(
    initial: RealAmount, flex: BudgetRule, transition: Transition,
    portfolio: Portfolio, targets: Weights,
    should_rebalance: Callable[[Observation], bool], reserve_years: float,
    assumptions: PreviewAssumptions,
) -> Initialize:
    def decide(obs: Observation, previous: RealAmount):
        budget = flex(obs, previous) if obs.month_index % 12 == 0 else previous
        actions = transition(obs, budget)  # Concrete terms/actions, not a cheaper-life flag.
        try:
            projected = preview(obs, actions, assumptions=assumptions)
        except PreviewError:
            # Submit the same proposal; execution identifies the fatal action.
            return Response(actions), budget

        # This author pays known due claims before discretionary consumption.
        for claim in projected.due_claims:
            funding = raise_cash(
                projected, portfolio, required_cash=claim.amount,
                lot_order="fifo", assumptions=assumptions,
            )
            payment = funding + (PayClaim(portfolio.cash, claim.id, claim.amount),)
            actions += payment
            try:
                projected = preview(projected, payment, assumptions=assumptions)
            except PreviewError:
                return Response(actions), budget

        consumption, projected = funded_consumption(
            projected, portfolio,
            projected.nominal(RealAmount(budget.value / 12, budget.basis)),
            assumptions=assumptions,
        )
        actions += consumption
        if projected is None:
            return Response(actions), budget
        if obs.month_index == 0 or should_rebalance(projected):
            actions += rebalance(
                projected, portfolio, targets=targets,
                retain_cash=projected.nominal(RealAmount(budget.value * reserve_years, budget.basis)),
                lot_order="fifo", assumptions=assumptions,
            )
        return Response(actions), budget

    def initialize(key: PolicyKey):
        return decide, initial
    return initialize


@dataclass(frozen=True)
class Inputs:
    situation: Situation
    household_id: str
    observations: NamedSeries
    models: dict[str, MarketModel]
    stocks: Instrument
    bonds: tuple[Instrument, ...]
    portfolio: Portfolio
    anchors: dict[str, RealAmount]
    flex_rules: dict[str, BudgetRule]
    transitions: dict[str, Transition]
    should_rebalance: Callable[[Observation], bool]
    reserve_years: float
    assumptions: PreviewAssumptions
    study_observers: dict[str, Observer]
    years: int
    paths: int


def spending_allocation(inputs: Inputs) -> StudyResult:
    household = inputs.situation.actor(inputs.household_id)
    rows: list[pl.DataFrame] = []
    runs: Runs = {}
    for model_name, model in inputs.models.items():
        worlds = model.condition(inputs.observations, at=inputs.situation.as_of).sample(
            years=inputs.years, step="month", paths=inputs.paths, seed=731,
        )
        for (anchor_name, anchor), (flex_name, flex), (move_name, transition), share, bonds in product(
            inputs.anchors.items(), inputs.flex_rules.items(), inputs.transitions.items(),
            (0.40, 0.60, 0.80, 1.0), inputs.bonds,
        ):
            initialize = household_policy(
                anchor, flex, transition, inputs.portfolio, {inputs.stocks: share, bonds: 1 - share},
                inputs.should_rebalance, inputs.reserve_years, inputs.assumptions,
            )
            run = run_paths(
                inputs.situation, policies={household: initialize}, reporting_actor=household,
                worlds=worlds,
                observers=financial_observers(
                    "total_spending_real", "minimum_annual_spending_real",
                    "terminal_wealth_real", "tax_paid_real",
                ) | inputs.study_observers,
            )
            key = model_name, anchor_name, flex_name, move_name, share, bonds.name
            runs[key] = run
            rows.append(run.paths.select(
                (~pl.col("reached_horizon")).mean().alias("stopped_fraction"),
                pl.col("contract_default").mean().alias("default_fraction"),
                pl.col("unfunded_withdrawal").mean().alias("spending_shortfall_fraction"),
                pl.col("backstop_used").mean().alias("backstop_fraction"),
                pl.col("total_spending_real").quantile(0.05).alias("p05_paid_through_stop"),
                pl.col("terminal_wealth_real").filter(pl.col("reached_horizon"))
                .quantile(0.05).alias("p05_terminal_completed"),
            ).with_columns(
                model=pl.lit(model_name), anchor=pl.lit(anchor_name),
                flex=pl.lit(flex_name), transition=pl.lit(move_name),
                stock_share=pl.lit(share), bonds=pl.lit(bonds.name),
            ))
    return pl.concat(rows), runs
```

These are ordinary editable functions: a guardrail, cash band or target trajectory
can be reused without adding a policy variant to the executor. The first useful
flex candidates are fixed real spending, bounded annual changes and GK-inspired
rules, with explicit consumption floors. This example reviews budgets annually
but **pays consumption monthly**; it does not change the annual published studies.

`Portfolio` supplies explicit accounts for every held/traded product, including
assets that need selling to establish a new target. Omitted target weights must
mean a declared full exit, not an invisible unchanged sleeve. The 100%-equity
duplicates can be collapsed in presentation. Initial allocation changes require
actual trades against the original book and taxes, not invented opening basis.

Previews do not execute anything or permit another callback. Each returned list
is submitted once. A failing move, payment, consumption or trade stops this path,
retaining earlier successful actions. Other rollouts continue. The helper's
immediate-cash or product-term assumptions must match supported execution rules;
pending proceeds cannot silently finance today's claim. Previewed tax consequences
use known inputs, not hidden future assessments. In the first one-household
immediate-cash control, scheduled cashflows and claim assembly precede the policy
observation. After its ordered actions, any still-unpaid due claim stops that
rollout. Broader product, housing and cross-actor timing remains scoped future
work; this sketch does not establish delayed settlement support.

## Budgets, backstops and receipt-aware behavior

Anchors exclude taxes, saving, debt principal and amounts already owed by contracts.
Reconcile the original budget so rent/mortgage costs cannot be counted twice.
Gross sales, consumption, taxes, transfers and reinvestment are separate cashflows.

A transition function can compare observed liquid wealth with several years of
the budget and return explicit actions on supplied terms. It reads prior receipts
and current contracts to avoid repeating a completed or pending transition. Merely
requesting a move is not evidence that it happened. More complex policies can keep
their own memory alongside the budget; no separate event-driven callback is needed.

A Europe backstop is not modeled by a cheap budget flag. It needs notice periods,
moving costs, lease/property actions, financing, residence/tax rules, FX and price
indices, including any option to return. These inputs and additional supported
actions remain a capability gap; no `Move` action is invented here. A budget-cut-only
cell is useful but cannot claim to model relocation. Unsupported relevant product
or jurisdiction treatment must reject assembly.

The situation carries current-year income/payments, filing units, accrued
liabilities, carryovers, enacted-law versions and explicit future assumptions.
Interest-only payouts, gross equity total-return proxies and untradable dated
bonds cannot be silently substituted for financially supported actual products.

`study_observers` supplies reductions for cuts, actual completed transitions,
backstop use and months in each lifestyle. Desired budget changes come from an
author-owned decision log; paid consumption and completed transitions come from
execution receipts, not a matching category label or intended action alone.

For any returned cell, compare `run.trace(path_id)` on the same path under another
policy. Replay initializes fresh memory keyed by original actor/path identity.
A sequence of monthly percentiles is not a trajectory. Report model panels,
paired sampling uncertainty, fit/vintage evidence, joint tails and stress behavior
separately; a model class name is not a quality certification.
