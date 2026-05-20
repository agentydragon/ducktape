from __future__ import annotations

from typing import Protocol

from augur.model.markets.scenarios import HistoricalSeries, Scenarios


class MarketModel(Protocol):
    """Model-neutral interface every candidate joint-time-series model implements.

    Phase A only exercises the likelihood path. `fit` and `simulate` are kept
    in the protocol so the same models are ready for the rollout-diagnostic
    phase later.
    """

    label: str
    factor_names: tuple[str, ...]

    def fit(self, historical: HistoricalSeries) -> None: ...

    def simulate(self, n_paths: int, n_months: int, seed: int) -> Scenarios: ...

    def log_predictive_density(self, historical: HistoricalSeries, t: int) -> float | None:
        """Log p(historical.levels[t+1] | historical.levels[:t+1]) under the
        currently fitted model. Returns None when the model can't expose a
        density (e.g. block bootstrap)."""
        ...

    def log_predictive_marginals(self, historical: HistoricalSeries, t: int) -> dict[str, float] | None:
        """Per-factor *marginal* univariate log-densities of
        `historical.levels[t+1] | historical.levels[:t+1]`.

        Sum is **not** equal to the joint log-density when factors are
        cross-correlated (cross-asset structure lives in the off-diagonal of
        the joint covariance). Useful as an interpretive breakdown of where a
        model's joint score comes from. Returns None when the model declines
        to expose marginals (e.g. block bootstrap).
        """
        ...

    def log_predictive_density_at_horizon(self, historical: HistoricalSeries, t: int, h: int) -> float | None:
        """Joint log-density of `historical.levels[t+h] | historical.levels[:t+1]`.

        h-step-ahead density: condition on history up to and including t+1
        (i.e. the levels through `t+1`), then predict the cumulative
        log-return over the next h months without seeing intermediate
        observations. Reveals structural differences (vol clustering,
        cointegration pull, cascade dynamics) that one-step-ahead density
        smooths over.

        Closed-form for VAR / Wilkie / VECM (multivariate Gaussian under the
        fitted recurrence). DCC / SV families don't have a closed form;
        they should fit a Gaussian to a Monte-Carlo cloud of h-step-ahead
        cumulative log-returns. Returns None when the model declines to
        expose an h-step density.
        """
        ...
