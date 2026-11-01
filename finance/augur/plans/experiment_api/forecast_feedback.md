# A policy that periodically runs its own planning calculation

Proposed Python. [Frank, Mitchell, and Blanchett (2011)](https://www.financialplanningassociation.org/article/journal/NOV11-probability-failure-based-decision-rules-manage-sequence-risk-retirement)
motivates responding to an evolving estimate of failure risk. This algorithm is
our extension, not their published implementation: annually estimate risk under
unchanged real spending, then optionally make one bounded adjustment. Consumption
remains monthly. The next annual review reassesses the changed situation.

The outer world and the policy's beliefs are distinct models. Crossing them tests
misspecification without exposing the outer sampled future.

```python
from collections.abc import Callable
from datetime import date
from itertools import product

import polars as pl

from proposed_augur.accounting import Actor
from proposed_augur.actions import Action
from proposed_augur.data import NamedSeries
from proposed_augur.markets import MarketModel, Worlds
from proposed_augur.money import RealAmount
from proposed_augur.observations import Observation
from proposed_augur.policies import Initialize, PolicyKey, Response, scalar_to_batch
from proposed_augur.results import Runs, StudyResult, financial_observers
from proposed_augur.simulation import run as run_paths
from proposed_augur.state import Situation

type PlanningSituation = Callable[[Observation, Worlds], Situation]
type FixedPolicy = Callable[[RealAmount], Initialize]
type MonthlyActions = Callable[[Observation, RealAmount], tuple[Action, ...]]
type PlanningSeed = Callable[[str, date, str], int]


def reassessed_policy(
    initial: RealAmount, belief: MarketModel, fixed_policy: FixedPolicy,
    monthly_actions: MonthlyActions, planning_situation: PlanningSituation,
    seed_for: PlanningSeed, lower: float, upper: float, inner_paths: int,
) -> Initialize:
    def initialize(key: PolicyKey):
        def decide(obs: Observation, previous: RealAmount):
            budget = previous
            if obs.month_index > 0 and obs.month_index % 12 == 0:
                inner_worlds = belief.condition(obs.market, at=obs.at).sample(
                    years=obs.months_remaining // 12, step="month", paths=inner_paths,
                    seed=seed_for(key.path_id, obs.at, "fixed-spending-check"),
                )
                known_situation = planning_situation(obs, inner_worlds)
                if known_situation.basis != initial.basis:
                    raise ValueError("An inner forecast must retain the original real-money basis")
                check = run_paths(
                    known_situation, policies={obs.actor: fixed_policy(previous)},
                    reporting_actor=obs.actor, worlds=inner_worlds, observers={},
                )
                risk = check.paths.select((~pl.col("reached_horizon")).mean()).item()
                factor = 0.95 if risk > upper else 1.025 if risk < lower else 1.0
                budget = RealAmount(previous.value * factor, previous.basis)
            return Response(monthly_actions(obs, budget)), budget

        return decide, initial
    return scalar_to_batch(initialize)


def forecast_feedback(
    situation: Situation, actor: Actor, models: dict[str, MarketModel],
    observations: NamedSeries, fixed_policy: FixedPolicy, monthly_actions: MonthlyActions,
    planning_situation: PlanningSituation, seed_for: PlanningSeed,
    *, initial: RealAmount, years: int, paths: int,
) -> StudyResult:
    rows: list[pl.DataFrame] = []
    runs: Runs = {}
    for outer_name, outer in models.items():
        worlds = outer.condition(observations, at=situation.as_of).sample(
            years=years, step="month", paths=paths, seed=2011,
        )
        for belief_name, (lower, upper), inner_paths in product(
            models, ((0.05, 0.15), (0.10, 0.25)), (256, 1024),
        ):
            initialize = reassessed_policy(
                initial, models[belief_name], fixed_policy, monthly_actions,
                planning_situation, seed_for, lower, upper, inner_paths,
            )
            run = run_paths(
                situation, policies={actor: initialize}, reporting_actor=actor, worlds=worlds,
                observers=financial_observers("total_spending_real", "terminal_wealth_real"),
            )
            failures = ~pl.col("reached_horizon")
            rows.append(run.paths.select(
                failures.mean().alias("failure_fraction"),
                (failures.cast(pl.Float64).std() / pl.len().sqrt()).alias("failure_se"),
                pl.col("total_spending_real").quantile(0.05).alias("p05_paid_through_stop"),
                pl.col("terminal_wealth_real").filter(pl.col("reached_horizon"))
                .median().alias("median_terminal_completed"),
            ).with_columns(
                outer_model=pl.lit(outer_name), belief_model=pl.lit(belief_name),
                lower=pl.lit(lower), upper=pl.lit(upper), inner_paths=pl.lit(inner_paths),
            ))
            runs[outer_name, belief_name, lower, upper, inner_paths] = run
    return pl.concat(rows), runs
```

`planning_situation` is an author-supplied assembly function from **actor-known**
books/lots, basis, known contracts, filing/payment records and explicit assumptions
about unknowns/counterparties. It is not `obs.checkpoints()` or permission to clone
the entire hidden executor. It must preserve liabilities, known pending settlement,
the original reporting basis and the remaining horizon. A missing capability to
represent those facts is an input/design gap, not a fresh tax-free investor.

The inner function holds the current real budget fixed and uses the same declared
stateless investment/claim-payment rules as `monthly_actions`; it does not
recursively reassess forecasts. Any stateful investment variant must explicitly
pass its actor-known memory to the inner initializer too. Inner scenario assembly
must not repeat initial portfolio establishment or housing acquisition already
completed in the outer world. Synthetic scenario month zero is not “start life
again.” This is conditional risk under unchanged spending, not risk of the
adaptive policy itself; outer outcomes measure the latter.

`monthly_actions` is ordinary composed Python proposal code, like the
[personal example](spending_allocation.md): explicit funding, payments, consumption
and investments. It is not an engine spending hook or target-weight instruction.
The canonical outer policy is called once per actor's monthly batch; its optional
scalar adapter evaluates each path once. Inner simulation is an
independent experiment computation, not another decision opportunity on the outer
books. Failed actions stop their own inner or outer rollout with prefix receipts
retained. There is no execution retry or exception-driven budget repair.

The belief model conditions only on dated observations available to the actor.
Calendar, products, currency and price-index identity must agree. Future inflation
comes from the belief model, not outer realizations. Fitted beliefs are fixed at
the declared training date; a learning variant needs an explicit refitting rule
using then-available evidence.

`seed_for` is a reproducible experiment-owned derivation from stable outer path,
review date and purpose, not row order or Python's process-randomized hash. Inner
streams are disjoint from outer streams and may be paired across policy cells
where meaningful. Selected replay uses the same identity with fresh memory.
Changing inner sample count may cause chatter; compare 256/1024 draws and log
budget decisions separately from paid receipts. Confidence-aware rules or offline
surrogates are later alternatives requiring measured approximation error.

This intentionally demanding example belongs in the runtime/authoring evaluation.
It permits nested computation without promising that arbitrary Python closures
compile or that scalar inner loops are fast. Both outer and inner run helpers
accept only batch policies. Batch representation and executor language remain
separate choices, not an option to expose a second scalar engine interface.
