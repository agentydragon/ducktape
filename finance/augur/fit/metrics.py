"""Held-out / rolling-origin / multi-step predictive metrics.

Every scorer calls `model.predictive(historical, t, horizon=h)` once per
origin and projects three quantities from the returned distribution:

  - joint log-density           (`augur.fit.scoring.joint_log_density`)
  - per-factor marginal log-density (`augur.fit.scoring.marginal_log_densities`)
  - per-factor CRPS              (`augur.fit.scoring.gaussian_crps`)

Scored results contain descriptive aggregates, which can be non-finite.
Unscored results give the unavailable-predictive reason, without an aggregate
over a partial subset. Rolling-origin and multi-step results retain completed
origin scores in either case and leave uncertainty explicitly unestimated.

Fitting is the caller's responsibility — scorers don't refit (except
rolling-origin, which refits at each origin via the supplied factory).
This lets a YAML-configured Scorable model (like `IndependentModel`)
plug into the same battery as a fittable model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from finance.augur.fit.model import FittableScorable, Scorable
from finance.augur.fit.scoring import gaussian_crps, joint_log_density, marginal_log_densities
from finance.augur.model.path_models.scenarios import DatedHistoricalSeries, HistoricalSeries

_HistoricalInput = HistoricalSeries | DatedHistoricalSeries


@dataclass(frozen=True)
class CalendarCoverage:
    """Observed calendar coverage and transitions eligible for monthly scoring."""

    observed_level_count: int
    contiguous_segment_count: int
    usable_transition_count: int
    cross_gap_pair_count: int
    omitted_month_count: int
    segments: tuple[tuple[str, str, int], ...]
    gaps: tuple[tuple[str, str, tuple[str, ...]], ...]


def _coverage(historical: DatedHistoricalSeries) -> CalendarCoverage:
    return CalendarCoverage(
        observed_level_count=len(historical.months),
        contiguous_segment_count=len(historical.segments),
        usable_transition_count=len(historical.transition_pairs),
        cross_gap_pair_count=len(historical.gaps),
        omitted_month_count=sum(len(gap.missing_months) for gap in historical.gaps),
        segments=tuple(
            (segment.months[0], segment.months[-1], segment.transition_count) for segment in historical.segments
        ),
        gaps=tuple((gap.before_month, gap.after_month, gap.missing_months) for gap in historical.gaps),
    )


def calendar_coverage(historical: DatedHistoricalSeries) -> CalendarCoverage:
    """Report observed spans and the cross-gap pairs excluded from monthly fitting/scoring."""
    return _coverage(historical)


@dataclass(frozen=True)
class _TransitionOrigin:
    """One observed one-month transition and the consecutive history containing it."""

    index: int
    start_row: int
    end_row: int
    segment: HistoricalSeries
    local_origin: int


def _transition_origins(historical: _HistoricalInput) -> tuple[_TransitionOrigin, ...]:
    """Flatten only real consecutive transitions while retaining their segment-local origins."""
    if isinstance(historical, DatedHistoricalSeries):
        segments = tuple(
            (
                segment.start_index,
                HistoricalSeries(series_names=historical.series_names, levels=segment.levels, months=segment.months),
            )
            for segment in historical.segments
            if segment.transition_count > 0
        )
    else:
        historical.require_consecutive_months()
        segments = ((0, historical),)

    origins: list[_TransitionOrigin] = []
    for start_row, segment in segments:
        for local_origin in range(len(segment.months) - 1):
            origins.append(
                _TransitionOrigin(
                    index=len(origins),
                    start_row=start_row + local_origin,
                    end_row=start_row + local_origin + 1,
                    segment=segment,
                    local_origin=local_origin,
                )
            )
    return tuple(origins)


def _calendar_coverage(historical: _HistoricalInput) -> CalendarCoverage | None:
    return _coverage(historical) if isinstance(historical, DatedHistoricalSeries) else None


def fit_for_scoring(model: FittableScorable, historical: _HistoricalInput) -> None:
    """Fit from real dated observations, requiring explicit support when gaps exist."""
    if isinstance(historical, HistoricalSeries):
        historical.require_consecutive_months()
        model.fit(historical)
        return
    # `fit_dated` is an optional model capability for preserving gaps in fitting.
    fit_dated = getattr(model, "fit_dated", None)
    if callable(fit_dated):
        fit_dated(historical)
        return
    if historical.gaps:
        raise TypeError(f"{model.label} does not support fitting dated histories with calendar gaps")
    model.fit(
        HistoricalSeries(series_names=historical.series_names, levels=historical.levels, months=historical.months)
    )


@dataclass(frozen=True)
class OriginScore:
    """Joint score at a historical origin, including non-finite outcomes unchanged."""

    origin_index: int
    origin_month: str
    joint_log_density: float


DEPENDENT_ORIGIN_UNCERTAINTY = (
    "Unestimated: forecast origins share a time series and training data, and multi-step targets may overlap. "
    "No dependence model or uncertainty estimator has been specified; score means do not establish a ranking."
)


def _summarise_scores(scores: list[OriginScore]) -> tuple[float, float]:
    """Descriptive total and mean over every origin; non-finite scores propagate."""
    total = float(sum(score.joint_log_density for score in scores))
    return total, total / len(scores)


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
    calendar_coverage: CalendarCoverage | None = None


@dataclass(frozen=True)
class UnscoredHeldOutResult:
    """Held-out: the model returned None from predictive(...) at some held-out month."""

    model_label: str
    train_end: int
    held_out_count: int
    unscored_reason: str
    calendar_coverage: CalendarCoverage | None = None


HeldOutResult = ScoredHeldOutResult | UnscoredHeldOutResult


def held_out_predictive_score(
    model: Scorable, historical: _HistoricalInput, *, train_fraction: float = 0.8
) -> HeldOutResult:
    """Score `model.predictive(...)` over the held-out window of `historical`.

    The caller is responsible for any required fitting before calling this:
    a `Fittable` model should be fit on `historical.levels[:train_end+1]`;
    a YAML-configured `Scorable` (e.g. `IndependentModel`) needs
    no fitting and is passed as-is. The split index `train_end` is
    computed from `train_fraction` and surfaced on the result for
    auditability.

    During scoring `predictive(history, local_origin, horizon=1)` is called
    with the full contiguous segment containing that origin. It is expected
    to condition on `history.levels[:local_origin+1]` to predict the next
    observed month. Origins are indexed globally across eligible transitions
    in the result, while each model call uses the segment-local index.
    """
    if not 0.0 < train_fraction < 1.0:
        raise ValueError(f"train_fraction must be in (0, 1); got {train_fraction}")
    transitions = _transition_origins(historical)
    n_steps = len(transitions)
    if n_steps < 2:
        raise ValueError("need at least two transitions to split into train and test")
    train_end = max(1, round(n_steps * train_fraction))
    if train_end >= n_steps:
        raise ValueError(
            f"train_fraction {train_fraction} leaves no held-out months (train_end={train_end}, n_steps={n_steps})"
        )

    held_out_count = n_steps - train_end
    factor_labels = tuple(factor.wire_id for factor in historical.series_names)
    log_densities: list[float] = []
    marginal_totals: dict[str, float] = dict.fromkeys(factor_labels, 0.0)
    crps_totals: dict[str, float] = dict.fromkeys(factor_labels, 0.0)
    log_levels = np.log(historical.levels)
    coverage = _calendar_coverage(historical)
    for transition in transitions[train_end:]:
        pred = model.predictive(transition.segment, transition.local_origin, horizon=1)
        if pred is None:
            return UnscoredHeldOutResult(
                model_label=model.label,
                train_end=train_end,
                held_out_count=held_out_count,
                unscored_reason=(
                    f"{model.label}.predictive returned None at {transition.segment.months[transition.local_origin]}"
                ),
                calendar_coverage=coverage,
            )
        observed = log_levels[transition.end_row] - log_levels[transition.start_row]
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
        calendar_coverage=coverage,
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
    origin_scores: tuple[OriginScore, ...]
    uncertainty_unestimated_reason: str
    factor_breakdown: FactorBreakdown | None
    calendar_coverage: CalendarCoverage | None = None


@dataclass(frozen=True)
class UnscoredRollingOriginResult:
    model_label: str
    min_train: int
    refit_every: int
    n_origins: int
    origin_scores: tuple[OriginScore, ...]
    unscored_reason: str
    calendar_coverage: CalendarCoverage | None = None


RollingOriginResult = ScoredRollingOriginResult | UnscoredRollingOriginResult


def rolling_origin_predictive_score(
    model_factory: Callable[[], FittableScorable],
    historical: _HistoricalInput,
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
    transitions = _transition_origins(historical)
    n_steps = len(transitions)
    if min_train >= n_steps:
        raise ValueError(f"min_train {min_train} leaves no held-out months (n_steps={n_steps})")

    label_holder = model_factory().label
    factor_labels = tuple(factor.wire_id for factor in historical.series_names)
    fit_cache: FittableScorable | None = None
    fit_origin: int | None = None
    origin_scores: list[OriginScore] = []
    marginal_totals: dict[str, float] = dict.fromkeys(factor_labels, 0.0)
    crps_totals: dict[str, float] = dict.fromkeys(factor_labels, 0.0)
    log_levels = np.log(historical.levels)
    coverage = _calendar_coverage(historical)

    for transition in transitions[min_train:]:
        t = transition.index
        if fit_cache is None or (t - min_train) % refit_every == 0:
            current_fit = model_factory()
            train_series: _HistoricalInput
            if isinstance(historical, DatedHistoricalSeries):
                train_series = historical.prefix_through(transition.start_row)
            else:
                train_series = HistoricalSeries(
                    series_names=historical.series_names,
                    levels=historical.levels[: transition.start_row + 1],
                    months=historical.months[: transition.start_row + 1],
                )
            fit_for_scoring(current_fit, train_series)
            fit_cache = current_fit
            fit_origin = t
        assert fit_cache is not None  # narrowing for type-checkers
        pred = fit_cache.predictive(transition.segment, transition.local_origin, horizon=1)
        if pred is None:
            return UnscoredRollingOriginResult(
                model_label=label_holder,
                min_train=min_train,
                refit_every=refit_every,
                n_origins=t - min_train,
                origin_scores=tuple(origin_scores),
                unscored_reason=(
                    f"{label_holder}.predictive returned None at "
                    f"{transition.segment.months[transition.local_origin]} (fit at transition {fit_origin})"
                ),
                calendar_coverage=coverage,
            )
        observed = log_levels[transition.end_row] - log_levels[transition.start_row]
        origin_scores.append(
            OriginScore(
                origin_index=t,
                origin_month=transition.segment.months[transition.local_origin],
                joint_log_density=joint_log_density(pred, observed),
            )
        )
        for name, value in marginal_log_densities(pred, observed, factor_labels).items():
            marginal_totals[name] += value
        for name, value in gaussian_crps(pred, observed, factor_labels).items():
            crps_totals[name] += value

    total, per_month = _summarise_scores(origin_scores)
    n_origins = len(origin_scores)
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
        origin_scores=tuple(origin_scores),
        uncertainty_unestimated_reason=DEPENDENT_ORIGIN_UNCERTAINTY,
        factor_breakdown=factor_breakdown,
        calendar_coverage=coverage,
    )


# ──────────────────────── Multi-step ────────────────────────


@dataclass(frozen=True)
class ScoredMultiStepRow:
    horizon_months: int
    n_origins: int
    joint_log_density_total: float
    joint_log_density_per_origin: float
    origin_scores: tuple[OriginScore, ...]
    uncertainty_unestimated_reason: str
    candidate_origins: int = 0
    excluded_cross_gap_origins: int = 0


@dataclass(frozen=True)
class UnscoredMultiStepRow:
    horizon_months: int
    n_origins: int
    origin_scores: tuple[OriginScore, ...]
    unscored_reason: str
    candidate_origins: int = 0
    excluded_cross_gap_origins: int = 0


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
    calendar_coverage: CalendarCoverage | None = None


def multi_step_predictive_score(
    model: Scorable,
    historical: _HistoricalInput,
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
    transitions = _transition_origins(historical)
    n_steps = len(transitions)
    train_end = max(1, round(n_steps * train_fraction))
    if train_end >= n_steps:
        raise ValueError(
            f"train_fraction {train_fraction} leaves no held-out months (train_end={train_end}, n_steps={n_steps})"
        )

    log_levels = np.log(historical.levels)
    horizon_rows: list[MultiStepRow] = []
    coverage = _calendar_coverage(historical)
    for h in horizons:
        scores: list[OriginScore] = []
        unscored_reason: str | None = None
        candidate_transitions = transitions[train_end : max(train_end, n_steps - h + 1)]
        excluded_cross_gap = 0
        for transition in candidate_transitions:
            if transition.local_origin + h > len(transition.segment.months) - 1:
                excluded_cross_gap += 1
                continue
            pred = model.predictive(transition.segment, transition.local_origin, horizon=h)
            if pred is None:
                unscored_reason = (
                    f"{model.label}.predictive returned None at "
                    f"{transition.segment.months[transition.local_origin]}, h={h}"
                )
                break
            observed = log_levels[transition.start_row + h] - log_levels[transition.start_row]
            scores.append(
                OriginScore(
                    origin_index=transition.index,
                    origin_month=transition.segment.months[transition.local_origin],
                    joint_log_density=joint_log_density(pred, observed),
                )
            )
        if unscored_reason is not None or not scores:
            horizon_rows.append(
                UnscoredMultiStepRow(
                    horizon_months=h,
                    n_origins=len(scores),
                    origin_scores=tuple(scores),
                    unscored_reason=unscored_reason
                    or (
                        f"no within-segment origins available for horizon {h} "
                        f"({excluded_cross_gap} crossed omitted calendar gaps)"
                    ),
                    candidate_origins=len(candidate_transitions),
                    excluded_cross_gap_origins=excluded_cross_gap,
                )
            )
            continue
        total, per_origin = _summarise_scores(scores)
        horizon_rows.append(
            ScoredMultiStepRow(
                horizon_months=h,
                n_origins=len(scores),
                joint_log_density_total=total,
                joint_log_density_per_origin=per_origin,
                origin_scores=tuple(scores),
                uncertainty_unestimated_reason=DEPENDENT_ORIGIN_UNCERTAINTY,
                candidate_origins=len(candidate_transitions),
                excluded_cross_gap_origins=excluded_cross_gap,
            )
        )

    return MultiStepResult(
        model_label=model.label,
        train_end=train_end,
        horizons=tuple(horizons),
        rows=tuple(horizon_rows),
        calendar_coverage=coverage,
    )
