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

from augur.markets.vecm import load_vecm
from augur.simulation import simulate
from augur.strategies import AnnualSpending, FixedWithdrawal, Strategy


def reassessed_spending(initial, index, belief, trading, lower, upper, inner_paths):
    initial_real = initial.to_number()

    def review(obs, previous_real):
        # A batch of continuations, each conditioned only on that outer path's past.
        forecasts = belief.condition(obs.market_history).sample_continuations(
            starts=obs.date, horizons=obs.remaining_horizon,
            paths_per_situation=inner_paths,
            streams=obs.random_stream("planning"),
        )
        check = simulate(
            obs.fork_situations(),
            Strategy(
                spending=FixedWithdrawal.from_real(
                    previous_real, index=index, interval="month", budget_period="year"
                ),
                trading=trading,
            ),
            worlds=forecasts, on_shortfall="stop", observe=(),
        )
        risk = check.by_starting_path.failure_fraction(
            events=("unfunded_withdrawal", "contract_default")
        )
        # Illustrative thresholds/actions, not a claim about the source paper.
        factor = np.where(risk > upper, 0.95, np.where(risk < lower, 1.025, 1.0))
        budget = np.where(obs.review_index == 0, initial_real, previous_real * factor)
        return budget, budget

    return AnnualSpending(
        review=review, initial_state=initial_real, price_index=index, consume_every="month"
    )


def forecast_feedback(situation, universe, artifacts, trading, *, initial, years, paths):
    rows, runs = [], {}
    for outer_name, artifact in artifacts.items():
        outer = load_vecm(artifact).bind(universe)
        worlds = outer.sample(start=situation.as_of, years=years, step="month", paths=paths, seed=2011)
        for belief_name, (lower, upper), inner_paths in product(
            artifacts, ((0.05, 0.15), (0.10, 0.25)), (256, 1024)
        ):
            belief = load_vecm(artifacts[belief_name]).bind(universe)
            spending = reassessed_spending(
                initial, outer.price_index, belief, trading, lower, upper, inner_paths
            )
            run = simulate(
                situation, Strategy(spending=spending, trading=trading), worlds=worlds,
                on_shortfall="stop", observe=("total_spending_real", "cuts", "terminal_wealth_real"),
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

`obs.fork_situations()` is a read-only fork at the decision point, before the
current withdrawal, including actual lots, accrued taxes, contracts, and remaining
horizon. The inner policy is fixed spending, so this is not unbounded recursion.
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
