# A policy that periodically runs its own planning calculation

Proposed Python. [Frank, Mitchell, and Blanchett (2011)](https://www.financialplanningassociation.org/article/journal/NOV11-probability-failure-based-decision-rules-manage-sequence-risk-retirement)
motivates responding to an evolving estimate of failure risk. The concrete
algorithm below is our extension, not their published implementation: once a
year, estimate failure risk under unchanged real spending, then optionally make
one bounded adjustment. The next review reassesses the changed situation.

There are two distinct models: the outer world being evaluated, and the policy's
beliefs when making decisions. Crossing them tests model misspecification. The
policy must never inspect the outer world's sampled future.

```python
from itertools import product

import numpy as np
import polars as pl

from proposed_augur.accounting import Actor
from proposed_augur.data import NamedSeries
from proposed_augur.markets import MarketModel
from proposed_augur.money import RealAmount, RealBatch
from proposed_augur.policies import BudgetDecision, InvestmentPolicy, Observations
from proposed_augur.results import Runs, StudyResult, count_budget_changes, financial_observers
from proposed_augur.simulation import resume, simulate
from proposed_augur.state import Situation
from proposed_augur.policies import AnnualSpending, FixedWithdrawal, Strategy


def reassessed_spending(
    initial: RealAmount, belief: MarketModel, trading: InvestmentPolicy,
    lower: float, upper: float, inner_paths: int,
) -> AnnualSpending:
    def review(obs: Observations, previous: RealBatch) -> BudgetDecision:
        # A batch of continuations, each conditioned only on that outer path's past.
        forecasts = belief.condition_many(obs.forecast_origins()).sample(
            paths_per_origin=inner_paths,
            streams=obs.random_stream("planning"),
        )
        check = resume(
            obs.checkpoints(),
            replace_policies={obs.actor: Strategy(
                spending=FixedWithdrawal.from_real(
                    previous, interval="month", budget_period="year"
                ),
                trading=trading,
            )},
            worlds=forecasts, on_shortfall="stop", observers={},
            reporting_actor=obs.actor,
        )
        risk = check.failure_fraction_by_origin(
            events=("unfunded_withdrawal", "contract_default")
        )
        # Illustrative thresholds/actions, not a claim about the source paper.
        factor = np.where(risk > upper, 0.95, np.where(risk < lower, 1.025, 1.0))
        budget = previous.with_values(np.where(obs.review_index == 0, initial.value, previous.values * factor))
        return BudgetDecision(budget=budget, state=budget)

    return AnnualSpending(
        review=review, initial_state=initial, consume_every="month"
    )


def forecast_feedback(
    situation: Situation, actor: Actor, models: dict[str, MarketModel], observations: NamedSeries,
    trading: InvestmentPolicy, *, initial: RealAmount, years: int, paths: int,
) -> StudyResult:
    rows: list[pl.DataFrame] = []
    runs: Runs = {}
    for outer_name, outer in models.items():
        worlds = outer.condition(observations, at=situation.as_of).sample(
            years=years, step="month", paths=paths, seed=2011
        )
        for belief_name, (lower, upper), inner_paths in product(
            models, ((0.05, 0.15), (0.10, 0.25)), (256, 1024)
        ):
            belief = models[belief_name]
            spending = reassessed_spending(
                initial, belief, trading, lower, upper, inner_paths
            )
            run = simulate(
                situation, policies={actor: Strategy(spending=spending, trading=trading)},
                reporting_actor=actor, worlds=worlds, on_shortfall="stop",
                observers=financial_observers("total_spending_real", "terminal_wealth_real")
                | {"cuts": count_budget_changes(direction="down")},
            )
            failures = pl.col("unfunded_withdrawal") | pl.col("contract_default")
            rows.append(run.paths.select(
                failures.mean().alias("failure_fraction"),
                (failures.cast(pl.Float64).std() / pl.len().sqrt()).alias("failure_se"),
                pl.col("total_spending_real").quantile(0.05).alias("p05_paid_through_stop"),
                pl.col("cuts").mean().alias("mean_cuts"),
            ).with_columns(
                outer_model=pl.lit(outer_name), belief_model=pl.lit(belief_name),
                lower=pl.lit(lower), upper=pl.lit(upper), inner_paths=pl.lit(inner_paths),
            ))
            runs[outer_name, belief_name, lower, upper, inner_paths] = run
    return pl.concat(rows), runs
```

`obs.checkpoints()` captures the decision point before the current withdrawal,
including actual lots, accrued taxes, contracts, policy memory, pending events,
reporting basis, and remaining horizon. `resume` clones that state per inner draw;
it does not initialize a new investor. Unchanged trading retains its memory; the
replacement spending policy starts from the supplied real annual budget. The
same base-date purchasing power applies on both sides of the fork: there is no
second rebasing or inflation adjustment at the origin. The inner policy is fixed
spending, so this is not unbounded recursion.
Its calculation is conditional risk if spending stays unchanged, not the risk
of the adaptive policy itself. Outer rollouts measure the latter.

The calendar and price-index identity must agree across models. Future inflation
in an inner forecast comes from the belief model, not the outer sampled path.
Fitted beliefs are fixed at the declared training date in this example; a learning
policy would need an explicit refitting rule and then-available evidence.

The inner sample-size sweep is important: noisy risk estimates can cause policy
chatter or change spending. Inner streams are disjoint from outer streams and
identified by outer path, review date, and purpose. Paired comparisons should
reuse inner draws where appropriate. Confidence-aware triggers and an offline
surrogate are later alternatives; a surrogate needs measured approximation error.

Nested forecasting makes this an intentionally demanding authoring example.
The design should permit it without promising it is cheap. This workload belongs
in any later comparison of scalar Python, batching/compilation, and native
execution; the interface should not force every policy into this cost model.
