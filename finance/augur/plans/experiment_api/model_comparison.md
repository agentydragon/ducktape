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
import polars as pl

from augur.datasets import align_monthly
from augur.scoring import LogGrowth, energy_score, variogram_score


def compare_forecasts(datasets, fitters, universe, origins, *, evaluation_as_of):
    # This experiment chooses these observable coordinates, not a global evidence bundle.
    projection = LogGrowth(columns=("equity_tr_index", "corporate_tr_index", "cpi"))
    scoring_series = {
        name: datasets[name].available_by(evaluation_as_of)
        for name in projection.columns
    }
    rows, fitted = [], {}
    for origin in origins:
        known = {name: series.available_by(origin) for name, series in datasets.items()}
        scale = projection.scale_from(known)  # Shared, learned only from training data.
        for model_name, fit in fitters.items():
            model = fit(known).bind(universe)
            fitted[origin, model_name] = model
            forecast = model.condition(known, at=origin).sample(
                start=origin, years=10, step="month", paths=4096, seed=410,
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
from augur.simulation import simulate


def evaluate_policies(situation, policies, worlds):
    rows, runs = [], {}
    for name, policy in policies.items():
        run = simulate(
            situation, policy, worlds=worlds, on_shortfall="stop",
            observe=("total_spending_real", "cuts", "backstop_used", "terminal_wealth_real"),
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


def compare_decisions(situation, models, policies, choose, *, years, paths):
    selections, selection_runs = {}, {}
    for name, model in models.items():
        worlds = model.sample(
            start=situation.as_of, years=years, step="month", paths=paths, seed=501
        )
        table, runs = evaluate_policies(situation, policies, worlds)
        selections[name] = choose(table, runs)  # Policy name, or None if none qualifies.
        selection_runs[name] = runs

    rows, evaluation_runs = [], {}
    for world_name, model in models.items():
        fresh_worlds = model.sample(
            start=situation.as_of, years=years, step="month", paths=paths, seed=502
        )
        table, runs = evaluate_policies(situation, policies, fresh_worlds)
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
