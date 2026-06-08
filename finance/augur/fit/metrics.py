"""Held-out / rolling-origin / multi-step predictive metrics.

Every scorer calls `model.predictive(historical, t, horizon=h)` once per
origin and projects three quantities from the returned distribution:

  - joint log-density           (`augur.fit.scoring.joint_log_density`)
  - per-factor marginal log-density (`augur.fit.scoring.marginal_log_densities`)
  - per-factor CRPS              (`augur.fit.scoring.gaussian_crps`)

Result types are discriminated unions: every score is either *scored* (all
numeric fields populated, no unscored_reason) or *unscored* (only
`unscored_reason` populated). The union shape makes invalid combinations
unrepresentable — see `HeldOutResult = ScoredHeldOutResult | UnscoredHeldOutResult`,
etc. Callers `isinstance(result, ScoredHeldOutResult)` to access numeric
fields.

Fitting is the caller's responsibility — scorers don't refit (except
rolling-origin, which refits at each origin via the supplied factory).
This lets a YAML-configured Scorable model (like `IndependentModel`)
plug into the same battery as a fittable model.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from finance.augur.fit.model import FittableScorable, Scorable
from finance.augur.fit.scoring import gaussian_crps, joint_log_density, marginal_log_densities
from finance.augur.model.path_models.scenarios import HistoricalSeries


def _summarise_scores(scores: list[float]) -> tuple[float, float, float]:
    """Return (total, per_origin, mean_se) for a list of per-origin scores.

    `mean_se` is the sample-stdev / √n estimate, NaN when fewer than two
    *finite* scores are available (single observation; or all-non-finite,
    e.g., an MC-fit predictive that exploded). Used by rolling-origin and
    multi-step scorers; held-out single-split summary just sums.
    """
    n = len(scores)
    total = float(sum(scores))
    per_origin = total / n
    finite = [s for s in scores if math.isfinite(s)]
    mean_se = float("nan") if len(finite) < 2 else statistics.stdev(finite) / math.sqrt(len(finite))
    return total, per_origin, mean_se


@dataclass(frozen=True)
class FactorBreakdown:
    """Per-factor projection: marginal log-density totals + CRPS totals.

    Sums of marginal log-densities don't equal the joint log-density when
    factors are cross-correlated — the off-diagonal of the joint covariance
    is what cointegration buys. CRPS doesn't have a joint summary; it's
    inherently per-factor (a multivariate generalisation exists — energy
    score — but isn't included here)."""

    marginal_log_density_total: dict[str, float]
    marginal_log_density_per_month: dict[str, float]
    crps_total: dict[str, float]
    crps_per_month: dict[str, float]


# ──────────────────────── Held-out single-split ────────────────────────


@dataclass(frozen=True)
class ScoredHeldOutResult:
    """Held-out predictive score, model scored cleanly. `factor_breakdown is
    None` only if the model's predictive isn't a closed-form MultivariateNormal
    (current models all return one, so in practice this is always populated)."""

    model_label: str
    train_end: int
    held_out_count: int
    joint_log_density_total: float
    joint_log_density_per_month: float
    factor_breakdown: FactorBreakdown | None


@dataclass(frozen=True)
class UnscoredHeldOutResult:
    """Held-out: the model returned None from predictive(...) at some held-out month."""

    model_label: str
    train_end: int
    held_out_count: int
    unscored_reason: str


HeldOutResult = ScoredHeldOutResult | UnscoredHeldOutResult


def held_out_predictive_score(
    model: Scorable, historical: HistoricalSeries, *, train_fraction: float = 0.8
) -> HeldOutResult:
    """Score `model.predictive(...)` over the held-out window of `historical`.

    The caller is responsible for any required fitting before calling this:
    a `Fittable` model should be fit on `historical.levels[:train_end+1]`;
    a YAML-configured `Scorable` (e.g. `IndependentModel`) needs
    no fitting and is passed as-is. The split index `train_end` is
    computed from `train_fraction` and surfaced on the result for
    auditability.

    During scoring `predictive(historical, t, horizon=1)` is called with the
    full series and is expected to condition on `levels[:t+1]` to predict
    `levels[t+1]`.
    """
    if not 0.0 < train_fraction < 1.0:
        raise ValueError(f"train_fraction must be in (0, 1); got {train_fraction}")
    n_steps = historical.levels.shape[0] - 1
    if n_steps < 2:
        raise ValueError("need at least two transitions to split into train and test")
    train_end = max(1, round(n_steps * train_fraction))
    if train_end >= n_steps:
        raise ValueError(
            f"train_fraction {train_fraction} leaves no held-out months (train_end={train_end}, n_steps={n_steps})"
        )

    held_out_count = n_steps - train_end
    # Report labels are the factors' wire ids — the metric breakdown is a human-readable
    # {factor_label: score} report; the typed LevelSeriesKey identity rides on `historical`.
    factor_labels = tuple(factor.wire_id for factor in historical.factor_names)
    log_densities: list[float] = []
    marginal_totals: dict[str, float] = dict.fromkeys(factor_labels, 0.0)
    crps_totals: dict[str, float] = dict.fromkeys(factor_labels, 0.0)
    log_levels = np.log(historical.levels)
    for t in range(train_end, n_steps):
        pred = model.predictive(historical, t, horizon=1)
        if pred is None:
            return UnscoredHeldOutResult(
                model_label=model.label,
                train_end=train_end,
                held_out_count=held_out_count,
                unscored_reason=f"{model.label}.predictive returned None at t={t}",
            )
        observed = log_levels[t + 1] - log_levels[t]
        log_densities.append(joint_log_density(pred, observed))
        for name, value in marginal_log_densities(pred, observed, factor_labels).items():
            marginal_totals[name] += value
        for name, value in gaussian_crps(pred, observed, factor_labels).items():
            crps_totals[name] += value

    total = float(sum(log_densities))
    factor_breakdown = FactorBreakdown(
        marginal_log_density_total=marginal_totals,
        marginal_log_density_per_month={name: value / held_out_count for name, value in marginal_totals.items()},
        crps_total=crps_totals,
        crps_per_month={name: value / held_out_count for name, value in crps_totals.items()},
    )
    return ScoredHeldOutResult(
        model_label=model.label,
        train_end=train_end,
        held_out_count=held_out_count,
        joint_log_density_total=total,
        joint_log_density_per_month=total / held_out_count,
        factor_breakdown=factor_breakdown,
    )


# ──────────────────────── Rolling-origin ────────────────────────


@dataclass(frozen=True)
class ScoredRollingOriginResult:
    """Rolling-origin predictive score, scored over `n_origins` origins."""

    model_label: str
    min_train: int
    refit_every: int
    n_origins: int
    joint_log_density_total: float
    joint_log_density_per_month: float
    joint_log_density_mean_se: float
    factor_breakdown: FactorBreakdown | None


@dataclass(frozen=True)
class UnscoredRollingOriginResult:
    model_label: str
    min_train: int
    refit_every: int
    n_origins: int
    unscored_reason: str


RollingOriginResult = ScoredRollingOriginResult | UnscoredRollingOriginResult


def rolling_origin_predictive_score(
    model_factory: Callable[[], FittableScorable],
    historical: HistoricalSeries,
    *,
    min_train: int = 60,
    refit_every: int = 1,
) -> RollingOriginResult:
    """Refit at each origin `t ∈ [min_train, n_steps)` (every `refit_every`
    steps to control cost on slow-fitting models like DCC-GARCH) and score
    the one-step-ahead predictive density at `t`.

    `model_factory()` must return a fresh `FittableScorable` instance —
    fits are independent and state must reset between origins. This is why
    rolling-origin needs Fittable but the single-split scorer above only
    needs Scorable (no refit, caller-supplied training).
    """
    if min_train < 2:
        raise ValueError(f"min_train must be >= 2; got {min_train}")
    if refit_every < 1:
        raise ValueError(f"refit_every must be >= 1; got {refit_every}")
    n_steps = historical.levels.shape[0] - 1
    if min_train >= n_steps:
        raise ValueError(f"min_train {min_train} leaves no held-out months (n_steps={n_steps})")

    label_holder = model_factory().label
    factor_labels = tuple(factor.wire_id for factor in historical.factor_names)
    fit_cache: FittableScorable | None = None
    fit_origin: int | None = None
    log_densities: list[float] = []
    marginal_totals: dict[str, float] = dict.fromkeys(factor_labels, 0.0)
    crps_totals: dict[str, float] = dict.fromkeys(factor_labels, 0.0)
    log_levels = np.log(historical.levels)

    for t in range(min_train, n_steps):
        if fit_cache is None or (t - min_train) % refit_every == 0:
            current_fit = model_factory()
            train_series = HistoricalSeries(
                factor_names=historical.factor_names,
                levels=historical.levels[: t + 1],
                months=historical.months[: t + 1],
            )
            current_fit.fit(train_series)
            fit_cache = current_fit
            fit_origin = t
        assert fit_cache is not None  # narrowing for type-checkers
        pred = fit_cache.predictive(historical, t, horizon=1)
        if pred is None:
            return UnscoredRollingOriginResult(
                model_label=label_holder,
                min_train=min_train,
                refit_every=refit_every,
                n_origins=t - min_train,
                unscored_reason=(f"{label_holder}.predictive returned None at t={t} (fit at origin {fit_origin})"),
            )
        observed = log_levels[t + 1] - log_levels[t]
        log_densities.append(joint_log_density(pred, observed))
        for name, value in marginal_log_densities(pred, observed, factor_labels).items():
            marginal_totals[name] += value
        for name, value in gaussian_crps(pred, observed, factor_labels).items():
            crps_totals[name] += value

    total, per_month, mean_se = _summarise_scores(log_densities)
    n_origins = len(log_densities)
    factor_breakdown = FactorBreakdown(
        marginal_log_density_total=marginal_totals,
        marginal_log_density_per_month={name: value / n_origins for name, value in marginal_totals.items()},
        crps_total=crps_totals,
        crps_per_month={name: value / n_origins for name, value in crps_totals.items()},
    )
    return ScoredRollingOriginResult(
        model_label=label_holder,
        min_train=min_train,
        refit_every=refit_every,
        n_origins=n_origins,
        joint_log_density_total=total,
        joint_log_density_per_month=per_month,
        joint_log_density_mean_se=mean_se,
        factor_breakdown=factor_breakdown,
    )


# ──────────────────────── Multi-step ────────────────────────


@dataclass(frozen=True)
class ScoredMultiStepRow:
    horizon_months: int
    n_origins: int
    joint_log_density_total: float
    joint_log_density_per_origin: float
    joint_log_density_mean_se: float


@dataclass(frozen=True)
class UnscoredMultiStepRow:
    horizon_months: int
    n_origins: int
    unscored_reason: str


MultiStepRow = ScoredMultiStepRow | UnscoredMultiStepRow


@dataclass(frozen=True)
class MultiStepResult:
    """Multi-step (cumulative h-month) predictive log-density per horizon.

    For each origin t in the held-out window and each horizon h in
    `horizons`, score the joint log-density of `Σ_{k=1..h} r_{t+k}` under
    the model's `predictive(historical, t, horizon=h)`. Reveals structural
    differences (vol clustering, cointegration pull, cascade dynamics)
    that one-step density smooths over.
    """

    model_label: str
    train_end: int
    horizons: tuple[int, ...]
    rows: tuple[MultiStepRow, ...]


def multi_step_predictive_score(
    model: Scorable,
    historical: HistoricalSeries,
    *,
    horizons: tuple[int, ...] = (1, 6, 12),
    train_fraction: float = 0.8,
) -> MultiStepResult:
    """Score `model.predictive(..., horizon=h)` at every origin in the
    held-out window with `t + h ≤ n_steps`. Caller is responsible for any
    required fit (see `held_out_predictive_score` for the rationale)."""
    if not 0.0 < train_fraction < 1.0:
        raise ValueError(f"train_fraction must be in (0, 1); got {train_fraction}")
    if not horizons:
        raise ValueError("horizons must be non-empty")
    n_steps = historical.levels.shape[0] - 1
    train_end = max(1, round(n_steps * train_fraction))
    if train_end >= n_steps:
        raise ValueError(
            f"train_fraction {train_fraction} leaves no held-out months (train_end={train_end}, n_steps={n_steps})"
        )

    log_levels = np.log(historical.levels)
    horizon_rows: list[MultiStepRow] = []
    for h in horizons:
        scores: list[float] = []
        unscored_reason: str | None = None
        for t in range(train_end, n_steps - h + 1):
            pred = model.predictive(historical, t, horizon=h)
            if pred is None:
                unscored_reason = f"{model.label}.predictive returned None at t={t}, h={h}"
                break
            observed = log_levels[t + h] - log_levels[t]
            scores.append(joint_log_density(pred, observed))
        if unscored_reason is not None or not scores:
            horizon_rows.append(
                UnscoredMultiStepRow(
                    horizon_months=h,
                    n_origins=0,
                    unscored_reason=unscored_reason or f"no origins available for horizon {h}",
                )
            )
            continue
        total, per_origin, mean_se = _summarise_scores(scores)
        horizon_rows.append(
            ScoredMultiStepRow(
                horizon_months=h,
                n_origins=len(scores),
                joint_log_density_total=total,
                joint_log_density_per_origin=per_origin,
                joint_log_density_mean_se=mean_se,
            )
        )

    return MultiStepResult(
        model_label=model.label, train_end=train_end, horizons=tuple(horizons), rows=tuple(horizon_rows)
    )
