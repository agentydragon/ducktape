# Market models: predictive performance and decision consequences

Proposed Python. This is a separate experiment family, not a spending-rule variant.
It asks both whether a joint market model predicts observations well and whether
its differences from other models change the financial policies we would choose.
Neither a good in-sample fit nor agreement on a withdrawal rate settles both questions.

## Fit, forecast, score on held-out observations

Candidate fitters are executable functions supplied by the experiment: an IID
joint baseline, block bootstrap, VAR/VECM, regime-switching model, or a model with
term-structure mechanics. Each chooses its own estimation procedure. The common
contract is a forecast of the same observable quantities, not identical latent
states or an identical training algorithm.

```python
from collections.abc import Mapping, Sequence
from datetime import date

import polars as pl

from proposed_augur.data import align_monthly
from proposed_augur.results import LogGrowth, energy_score, variogram_score
from proposed_augur.data import NamedSeries
from proposed_augur.markets import Fitters, MarketModel


def compare_forecasts(
    datasets: NamedSeries, fitters: Fitters, origins: Sequence[date], *, evaluation_as_of: date,
) -> tuple[pl.DataFrame, pl.DataFrame, dict[tuple[date, str], MarketModel]]:
    # This experiment chooses these observable coordinates, not a global evidence bundle.
    projection = LogGrowth(columns=("equity_tr_index", "corporate_tr_index", "cpi"))
    scoring_series = {
        name: datasets[name].available_by(evaluation_as_of)
        for name in projection.columns
    }
    rows: list[dict[str, str | date | int | float]] = []
    fitted: dict[tuple[date, str], MarketModel] = {}
    for origin in origins:
        known = {name: series.available_by(origin) for name, series in datasets.items()}
        scale = projection.scale_from(known)  # Shared, learned only from training data.
        for model_name, fit in fitters.items():
            model = fit(known)  # Each fitter returns a model with its explicit financial binding.
            fitted[origin, model_name] = model
            forecast = model.condition(known, at=origin).sample(
                years=10, step="month", paths=4096, seed=410,
            )
            for months in (1, 12, 60, 120):
                observed = align_monthly(
                    scoring_series, start=origin, months=months, include_start=True, missing="raise"
                )
                samples = projection.forecast(forecast.prefix(months=months)) / scale
                actual = projection.observed(observed) / scale
                rows.append({
                    "model": model_name, "origin": origin, "months": months,
                    "energy": energy_score(samples, actual),
                    "variogram": variogram_score(samples, actual, power=0.5),
                })
    scores = pl.DataFrame(rows)
    means = scores.group_by("model", "months").agg(
        pl.col("energy").mean(), pl.col("variogram").mean(), pl.len().alias("origins")
    )
    return scores, means, fitted
```

The caller loads and names `datasets`, as in [the loading shell](exogenous.md).
Each fitter explicitly chooses its columns and transformations; it receives no
automatically selected datasets. Candidate bindings expose the three named index
level paths consumed here. The `LogGrowth` projection computes each column's
log end/start ratio, in the declared order, for both forecasts and observations.
There are no implicit yield or other macro coordinates in this example. A study
scoring yield changes would name those datasets and add those transformations.
Score additional path projections such as drawdowns and prolonged inflation
separately; terminal marginals alone miss sequence risk. The same transformation,
units, target record, origins, and scale apply to every candidate. All selected
origins must have complete outcomes at every compared horizon. Comparing
likelihoods of differently transformed latent states would not be a fair contest.

Energy and variogram scores are complementary sample-based forecast scores;
the latter helps test dependence errors. Lower is better with the loss convention
used here. See [Gneiting and Raftery (2007)](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf)
and [Scheuerer and Hamill (2015)](https://repository.library.noaa.gov/view/noaa/22327).
Also inspect marginal calibration/interval coverage, autocorrelation, stock/bond
co-losses, and persistence of inflation/rate regimes. These diagnostics complement
proper scores; picking a model by whichever diagnostic happens to favor it is not
a prespecified evaluation.

Keep the per-origin paired scores. Overlapping multi-year targets need dependence-
aware uncertainty, not an IID standard error across origins. Model tuning needs an
inner training/validation split; final evaluation periods must remain untouched.
Record whether evidence is release-vintage or revised: a truncated revised series
does not reproduce what a forecaster actually knew. In-sample fit statistics may
be reported separately, never substituted for this held-out evaluation.
`evaluation_as_of` freezes each target dataset's scoring vintage; those later
observations reach only the scorer, never fitting, scaling or conditioning.

## Which model would select which policy, and how fragile is that selection?

This shell uses an explicit finite set of authored policies, including spending
and allocation together. `choose` is the user's selection criterion over reported
outcomes: for example, a spending objective subject to agreed limits on cuts,
backstop use, and default. There is no model-independent "recommend" method.

```python
from proposed_augur.accounting import Actor
from proposed_augur.markets import Forecast, Worlds
from proposed_augur.policies import Strategy
from proposed_augur.results import PolicySelector, Run, count_budget_changes, ever_accepted_tag, financial_observers
from proposed_augur.simulation import simulate
from proposed_augur.state import Situation


def evaluate_policies(
    situation: Situation, actor: Actor, policies: Mapping[str, Strategy], worlds: Worlds,
) -> tuple[pl.DataFrame, dict[str, Run]]:
    rows: list[pl.DataFrame] = []
    runs: dict[str, Run] = {}
    for name, policy in policies.items():
        run = simulate(
            situation, policies={actor: policy}, reporting_actor=actor, worlds=worlds, on_shortfall="stop",
            observers=financial_observers("total_spending_real", "terminal_wealth_real") | {
                "cuts": count_budget_changes(direction="down"),
                "backstop_used": ever_accepted_tag("backstop"),
            },
        )
        failure = pl.col("unfunded_withdrawal") | pl.col("contract_default")
        rows.append(run.paths.select(
            failure.mean().alias("failure_fraction"),
            (failure.cast(pl.Float64).std() / pl.len().sqrt()).alias("failure_se"),
            pl.col("total_spending_real").mean().alias("mean_paid_through_stop"),
            pl.col("total_spending_real").quantile(0.05).alias("p05_paid_through_stop"),
            pl.col("cuts").mean().alias("mean_cuts"),
            pl.col("backstop_used").mean().alias("backstop_fraction"),
            pl.col("terminal_wealth_real").filter(pl.col("reached_horizon"))
            .median().alias("median_terminal_completed"),
        ).with_columns(policy=pl.lit(name)))
        runs[name] = run
    return pl.concat(rows), runs


def compare_decisions(
    situation: Situation, actor: Actor, models: Mapping[str, Forecast],
    policies: Mapping[str, Strategy], choose: PolicySelector, *, years: int, paths: int,
) -> tuple[dict[str, str | None], pl.DataFrame, dict[str, dict[str, Run]], dict[str, dict[str, Run]]]:
    selections: dict[str, str | None] = {}
    selection_runs: dict[str, dict[str, Run]] = {}
    for name, model in models.items():
        worlds = model.sample(
            years=years, step="month", paths=paths, seed=501
        )
        table, runs = evaluate_policies(situation, actor, policies, worlds)
        selections[name] = choose(table, runs)  # Policy name, or None if none qualifies.
        if selections[name] is not None and selections[name] not in policies:
            raise ValueError("The selector returned a policy outside the supplied grid")
        selection_runs[name] = runs

    rows: list[pl.DataFrame] = []
    evaluation_runs: dict[str, dict[str, Run]] = {}
    for world_name, model in models.items():
        fresh_worlds = model.sample(
            years=years, step="month", paths=paths, seed=502
        )
        table, runs = evaluate_policies(situation, actor, policies, fresh_worlds)
        evaluation_runs[world_name] = runs
        for chooser_name, selected in selections.items():
            if selected is not None:
                rows.append(table.filter(pl.col("policy") == selected).with_columns(
                    selected_under=pl.lit(chooser_name), evaluated_under=pl.lit(world_name)
                ))
    matrix = pl.concat(rows) if rows else pl.DataFrame()
    return selections, matrix, selection_runs, evaluation_runs
```

The matrix separates policy-selection noise from evaluation draws and exposes
model disagreement. All policies remain available, not only selection winners.
Compare policies on paired paths within each world model; report uncertainty in
objectives and constraints, not just a sorted list. `None` is an explicit infeasible
grid under the supplied criterion, not permission to relax spending or risk limits.
All models must have been fitted and conditioned as of the decision date. None is
treated as ground truth or implicitly assigned an ensemble probability. Robust or
minimax selection is another explicit criterion, not a default.

## Mechanics as well as fit

Candidate model families and research leads are in [the study map](studies.md).
Yield-curve models must value the actual durations/coupons used by the experiment;
matching a bond-return marginal is not a substitute for that consistency. A pricing
measure used for valuation is not the real-world probability measure used to
estimate spending outcomes. Preserve that distinction when using market prices
to constrain a forecast.

Today's [rolling-origin macro-window study](../../study/macro_window/holdout.py)
already supplies a useful precedent for isolating fit-window effects and scoring
at multiple horizons. This draft asks for broader model-family and decision
comparisons; it does not claim that work needs replacing.
