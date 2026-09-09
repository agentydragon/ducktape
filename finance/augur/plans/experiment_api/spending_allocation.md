# Personal planning: spending flexibility and allocation together

Proposed Python. This is a new experiment, not a published-study reproduction.
Its inputs live downstream: a dated opening balance sheet, tax lots, cash already
received, outstanding taxes, contracts, spending anchors, and transition terms.
No values here describe an actual account or person.

Vary initial spending, reversible flexibility, optional lifestyle transitions,
equity weight, and the kind of bonds purchased. Repeat under separately identified
market models. Inspect actual spending, transition use, default, and residual
wealth together. No single optimum is inferred without a preference criterion.

```python
from dataclasses import dataclass
from itertools import product

import polars as pl

from proposed_augur.instruments import InvestableUniverse
from proposed_augur.markets import MarketModel
from proposed_augur.policies import ActionReview, BudgetReview
from proposed_augur.simulation import simulate
from proposed_augur.state import Situation
from proposed_augur.policies import Allocation, CashReserve, DriftBand, LifestylePlan, SpendingAnchor, Strategy
from proposed_augur.data import NamedSeries
from proposed_augur.results import Runs, StudyResult, financial_observers, count_budget_changes, count_accepted_tag, ever_accepted_tag, months_per_lifestyle


@dataclass(frozen=True)
class Inputs:
    situation: Situation
    household_id: str
    observations: NamedSeries
    universe: InvestableUniverse
    models: dict[str, MarketModel]
    anchors: tuple[SpendingAnchor, ...]
    flex_rules: dict[str, BudgetReview]
    transition_rules: dict[str, ActionReview]
    lifestyles: LifestylePlan
    reserve: CashReserve
    rebalance: DriftBand
    years: int
    paths: int


def spending_allocation(inputs: Inputs) -> StudyResult:
    household = inputs.situation.actor(inputs.household_id)
    stocks = inputs.universe.instrument("broad_equity_fund")
    bond_options = (
        inputs.universe.instrument("short_treasury_fund"),
        inputs.universe.instrument("intermediate_treasury_fund"),
        inputs.universe.instrument("municipal_fund"),
    )
    rows: list[pl.DataFrame] = []
    runs: Runs = {}

    for model_name, model in inputs.models.items():
        market = model.condition(inputs.observations, at=inputs.situation.as_of)
        worlds = market.sample(
            years=inputs.years,
            step="month", paths=inputs.paths, seed=731,
        )
        for anchor, (flex_name, flex), (transition_name, transition), stock_share, bonds in product(
            inputs.anchors, inputs.flex_rules.items(), inputs.transition_rules.items(),
            (0.40, 0.60, 0.80, 1.0), bond_options,
        ):
            strategy = Strategy(
                spending=inputs.lifestyles.policy(
                    initial_anchor=anchor, flex=flex, transitions=transition,
                    review_every="year", consume_every="month",
                ),
                trading=Allocation(
                    target={stocks: stock_share, bonds: 1 - stock_share},
                    reserve=inputs.reserve,
                    rebalance=inputs.rebalance,
                    establish_target="trade_from_opening_book",
                    reinvest_surplus=True,
                    lot_selection="fifo",
                    transaction_costs=inputs.universe.execution_costs,
                ),
            )
            run = simulate(
                inputs.situation, policies={household: strategy}, reporting_actor=household, worlds=worlds,
                on_shortfall="stop",
                observers=financial_observers(
                    "total_spending_real", "minimum_annual_spending_real",
                    "terminal_wealth_real", "tax_paid_real",
                ) | {
                    "cuts": count_budget_changes(direction="down"),
                    "lifestyle_transitions": count_accepted_tag("lifestyle.changed"),
                    "backstop_used": ever_accepted_tag("backstop"),
                    "months_per_lifestyle": months_per_lifestyle(),
                },
            )
            key = (model_name, anchor.name, flex_name, transition_name, stock_share, bonds.name)
            runs[key] = run
            rows.append(
                run.paths.select(
                    pl.col("contract_default").mean().alias("default_fraction"),
                    pl.col("unfunded_withdrawal").mean().alias("spending_shortfall_fraction"),
                    (pl.col("unfunded_withdrawal").cast(pl.Float64).std() / pl.len().sqrt())
                    .alias("spending_shortfall_se"),
                    pl.col("backstop_used").mean().alias("backstop_fraction"),
                    pl.col("reached_horizon").mean().alias("completed_fraction"),
                    pl.col("total_spending_real").quantile(0.05).alias("p05_paid_through_stop"),
                    pl.col("terminal_wealth_real").filter(pl.col("reached_horizon"))
                    .quantile(0.05).alias("p05_terminal_wealth_completed"),
                    pl.col("terminal_wealth_real").filter(pl.col("reached_horizon"))
                    .median().alias("median_terminal_wealth_completed"),
                ).with_columns(
                    model=pl.lit(model_name), anchor=pl.lit(anchor.name),
                    flex=pl.lit(flex_name), transition=pl.lit(transition_name),
                    stock_share=pl.lit(stock_share), bonds=pl.lit(bonds.name),
                )
            )

    return pl.concat(rows), runs
```

The equity weights are illustrative sweep points. The 100%-equity duplicates can
be collapsed for presentation; equivalent cells remain the same strategy. The
portfolio starts from the supplied book in every cell. Establishing a target
executes purchases and any required sales, with taxes and costs. It does not
replace existing holdings with invented basis or assume existing tax bills were
paid. Fees and fund distribution treatment come from the shared instruments.

## What the spending inputs mean

`SpendingAnchor` supplies a named initial consumption budget and its price index.
It excludes taxes, saving, debt principal, and costs already charged by contracts.
A supplied budget adapter must reconcile those categories against the original
budget so mortgage/rent payments cannot be counted twice.

`flex_rules` maps report labels to executable budget-review functions, not schema
variants. The first useful candidates are fixed real spending, bounded real annual
changes, and Guyton–Klinger-style adjustments, with an explicit minimum consumption
amount. `BudgetReview` and `ActionReview` name callback signatures, not closed unions
of built-in behaviors. The author can define these functions in the experiment.
The observation used to size a budget includes known upcoming payments and the
tax consequences of proposed funding, through shared execution queries.

`LifestylePlan` is a small menu of consumption costs and available transitions.
For example, a move transition names its trigger, notice period, moving costs,
lease termination, property action if needed, and new consumption schedule.
Whether returning is possible and its costs are configured. A transition requests
those actions; their contractual and tax effects remain shared financial code.
If the move is unaffordable or disallowed, the trace records the rejection.
This does not require an autonomous-agent model of movers or landlords.

For example, the transition callback can express a simple liquid-runway trigger:

```python
from proposed_augur.policies import ActionDecision, ActionState, Move, MoveTerms, Observations


def runway_backstop(terms: MoveTerms, runway_years: float) -> ActionReview:
    def review(obs: Observations, state: ActionState) -> ActionDecision:
        request = (
            (obs.liquid_wealth_real.values < runway_years * obs.annual_budget_real.values)
            & ~obs.move_pending & ~obs.at_destination(terms.destination)
        )
        return ActionDecision(Move.request(terms=terms, where=request, accepted_tag="backstop"), state)
    return review
```

This is an illustrative trigger, not an endorsed safety threshold. `terms` carries
the actual notice, costs, contractual actions and residency change; the callback
does not merely flip a cheaper-lifestyle flag. No relocation and different runway
thresholds are ordinary cells. With zero/negative wealth, requesting a move is not
proof it can be financed; execution can reject it.

A transition changing jurisdiction also needs a supplied tax-residency timeline,
relevant cross-border rules, price indices, and FX model. Assembly must reject an
incomplete move scenario. A lower budget alone may be studied, but its name cannot
claim it models relocation. This draft supplies no country choice or tax rules.

`Situation` supplies a dated tax plan covering the run: starting-year income and
payments, accrued liabilities, lots, filing units, enacted-law version, future
indexation assumptions, and payment schedules. Unsupported relevant income or
instrument treatment is an error. Gross withdrawals, consumption, taxes, and
reinvestment are distinct cashflows.

## Looking beyond the summary table

For any returned cell, examine distributions of annual cuts, months in each
lifestyle, and tax paid from `run.paths`. `run.trace(path_id)` returns that
world's monthly financial events, observations, policy decisions, and balances.
To explain a difference, select the same `path_id` from two cells under the same
model and compare those traces. A path selected by terminal percentile is an
actual trajectory; the sequence of monthly percentiles is not one trajectory.

The study reports frequencies under each model separately. No model is designated
"institutional quality" by its class name: calibration evidence, fit window,
joint dynamics, stress behavior, and parameter uncertainty accompany the supplied
artifact and remain reviewable modeling inputs.
