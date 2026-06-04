"""Model-agnostic sanity checks for sampled exogenous trajectories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import yaml
from pydantic import Field, TypeAdapter, model_validator

from augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    level_series_request_channels,
    validate_sample_satisfies_request,
)
from augur.model.provider_config import (
    CompositeProviderConfig,
    MirroringProviderConfig,
    ProviderConfig,
    StateSpaceProviderConfig,
    TrainedPrivateEquityProviderConfig,
    VecmProviderConfig,
)
from augur.model.provider_includes import resolve_provider_includes
from augur.model.schemas import FrozenModel
from augur.model.series import IssuerId, LevelSeriesKey, PrivateEquityEventKindCode
from util.bazel.runfiles import get_required_path

_ADAPTER: TypeAdapter[ProviderConfig] = TypeAdapter(ProviderConfig)


class PercentileBound(FrozenModel):
    percentile: float = Field(ge=0.0, le=100.0)
    month: int = Field(ge=0)
    lower: float | None = None
    upper: float | None = None


class PercentileRangeBound(FrozenModel):
    lower_percentile: float = Field(ge=0.0, le=100.0)
    upper_percentile: float = Field(ge=0.0, le=100.0)
    month: int = Field(ge=0)
    lower: float
    upper: float

    @model_validator(mode="after")
    def _validate_ordering(self) -> PercentileRangeBound:
        if self.lower_percentile > self.upper_percentile:
            raise ValueError("lower_percentile must be <= upper_percentile")
        if self.lower > self.upper:
            raise ValueError("lower must be <= upper")
        return self


class EventCountPercentileBound(FrozenModel):
    percentile: float = Field(ge=0.0, le=100.0)
    lower: float | None = None
    upper: float | None = None


class EventCountPercentileRangeBound(FrozenModel):
    lower_percentile: float = Field(ge=0.0, le=100.0)
    upper_percentile: float = Field(ge=0.0, le=100.0)
    lower: float
    upper: float

    @model_validator(mode="after")
    def _validate_ordering(self) -> EventCountPercentileRangeBound:
        if self.lower_percentile > self.upper_percentile:
            raise ValueError("lower_percentile must be <= upper_percentile")
        if self.lower > self.upper:
            raise ValueError("lower must be <= upper")
        return self


class LevelThresholdProbabilityBound(FrozenModel):
    """Acceptance band on the fraction of rollouts whose level at `month` satisfies a threshold.

    Threshold semantics:
    - `absolute`: compare against `threshold` directly (same units as the series).
    - `ratio_of_initial`: compare against `threshold * level_at_month_0`.

    `comparison` is the predicate that defines the success event whose probability is bounded.
    """

    month: int = Field(ge=0)
    comparison: Literal["lt", "le", "gt", "ge"]
    threshold_kind: Literal["absolute", "ratio_of_initial"]
    threshold: float
    probability_lower: float = Field(ge=0.0, le=1.0)
    probability_upper: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_probability_ordering(self) -> LevelThresholdProbabilityBound:
        if self.probability_lower > self.probability_upper:
            raise ValueError("probability_lower must be <= probability_upper")
        return self


class LevelSeriesSanityCheck(FrozenModel):
    key: LevelSeriesKey
    initial_value: float | None = None
    initial_atol: float = Field(default=1e-6, ge=0.0)
    initial_rtol: float = Field(default=1e-9, ge=0.0)
    require_positive: bool = True
    value_percentile_bounds: tuple[PercentileBound, ...] = ()
    value_percentile_ranges: tuple[PercentileRangeBound, ...] = ()
    ratio_percentile_bounds: tuple[PercentileBound, ...] = ()
    ratio_percentile_ranges: tuple[PercentileRangeBound, ...] = ()
    threshold_probability_bounds: tuple[LevelThresholdProbabilityBound, ...] = ()


class EventSeriesSanityCheck(FrozenModel):
    """Sanity check on the `sale_opportunity_active` channel of one PE issuer."""

    issuer_id: IssuerId
    active_count_percentile_bounds: tuple[EventCountPercentileBound, ...] = ()
    active_count_percentile_ranges: tuple[EventCountPercentileRangeBound, ...] = ()


class EventKindObservedCheck(FrozenModel):
    """Acceptance band on the fraction of rollouts whose `event_kind_code` channel
    contains at least one (or exactly zero) occurrence of any code in `event_kind_codes`
    within the inclusive month window `[0, by_month]`.

    Multiple codes are treated as a union (OR), so the same check can express
    "at least one TENDER or PUBLIC_MARKET_OPEN by month 24" by listing both codes.
    """

    issuer_id: IssuerId
    event_kind_codes: tuple[int, ...] = Field(min_length=1)
    by_month: int = Field(ge=0)
    count_op: Literal["at_least_one", "exactly_zero"]
    probability_lower: float = Field(ge=0.0, le=1.0)
    probability_upper: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate(self) -> EventKindObservedCheck:
        if self.probability_lower > self.probability_upper:
            raise ValueError("probability_lower must be <= probability_upper")
        allowed = {int(code) for code in PrivateEquityEventKindCode}
        unexpected = sorted(set(self.event_kind_codes) - allowed)
        if unexpected:
            raise ValueError(f"event_kind_codes {unexpected} are not PrivateEquityEventKindCode values")
        return self


class PrivateEquityProtocolSanityCheck(FrozenModel):
    issuer_id: IssuerId
    allowed_regime_codes: tuple[int, ...] = ()
    allowed_event_kind_codes: tuple[int, ...] = ()


class PrivateEquityMarkSanityCheck(FrozenModel):
    """Sanity check on the `mark_usd_per_unit` channel of one PE issuer.

    Same statistical shape as `LevelSeriesSanityCheck` but sourced from the
    typed `PrivateEquityBundle` instead of the level matrix, since PE marks
    are not level series.
    """

    issuer_id: IssuerId
    initial_value: float | None = None
    initial_atol: float = Field(default=1e-6, ge=0.0)
    initial_rtol: float = Field(default=1e-9, ge=0.0)
    require_positive: bool = True
    ratio_percentile_bounds: tuple[PercentileBound, ...] = ()
    ratio_percentile_ranges: tuple[PercentileRangeBound, ...] = ()
    threshold_probability_bounds: tuple[LevelThresholdProbabilityBound, ...] = ()


class SampleSanitySpec(FrozenModel):
    """Reasonableness-band specification for a deployment's sampled bundle.

    The set of series/issuers to attempt is derived from the `*_checks` fields — each check
    declares the key/issuer it bounds and is therefore implicitly a request to sample that
    series. A check whose key the model can't emit is surfaced as `unmodeled` per band rather
    than treated as a hard sampling failure; the field used to be `required_level_series` /
    `required_private_equity_issuers` and gated the sample request, but a sanity-band YAML
    listing a series the deployment's model doesn't cover was meant to render as "not modeled
    by <preset>", not 400 the calibration tab.
    """

    provider_config_path: Path
    horizon_months: int = Field(ge=0)
    rollout_seed_start: int = Field(default=1301, ge=0)
    rollout_count: int = Field(gt=0)
    level_checks: tuple[LevelSeriesSanityCheck, ...] = ()
    event_checks: tuple[EventSeriesSanityCheck, ...] = ()
    event_kind_observed_checks: tuple[EventKindObservedCheck, ...] = ()
    private_equity_protocol_checks: tuple[PrivateEquityProtocolSanityCheck, ...] = ()
    private_equity_mark_checks: tuple[PrivateEquityMarkSanityCheck, ...] = ()

    @property
    def rollout_seeds(self) -> tuple[int, ...]:
        return tuple(range(self.rollout_seed_start, self.rollout_seed_start + self.rollout_count))

    @property
    def attempted_level_keys(self) -> frozenset[LevelSeriesKey]:
        """Level keys this spec bounds, derived from `level_checks[*].key`."""

        return frozenset(check.key for check in self.level_checks)

    @property
    def attempted_private_equity_issuers(self) -> frozenset[IssuerId]:
        """PE issuers this spec bounds, derived from every check carrying an `issuer_id`."""
        # Accumulate per check-type so mypy sees each concrete `issuer_id`; a single combined
        # comprehension widens the loop variable to the common `FrozenModel` base, which has none.
        issuers: set[IssuerId] = set()
        issuers.update(check.issuer_id for check in self.event_checks)
        issuers.update(check.issuer_id for check in self.event_kind_observed_checks)
        issuers.update(check.issuer_id for check in self.private_equity_protocol_checks)
        issuers.update(check.issuer_id for check in self.private_equity_mark_checks)
        return frozenset(issuers)


@dataclass(frozen=True)
class SanityBandResult:
    """One evaluated sanity check: a labeled expected band vs the observed value(s)."""

    label: str  # human-readable, e.g. "sp500 ratio m12/m0 p1..p99"
    series_id: str  # the level-series wire id or "PE issuer 'openai' mark"
    kind: str  # "anchor"|"percentile_bound"|"percentile_range"|"threshold_probability"|"count_range"|"event_kind_probability"|"codes_allowed"|"unmodeled"
    month: int | None  # month index where applicable, else None
    expected_lower: float | None
    expected_upper: float | None
    observed: tuple[float, ...]  # the value(s) bounded: 1 for bound/probability, 2 for a range (lo, hi pctile values)
    observed_labels: tuple[str, ...]  # parallel labels, e.g. ("p1","p99") or ("p50",) or ("probability",)
    # `unmodeled` = the spec asked for this series/issuer but the deployment's preset can't
    # emit it (e.g. a state-space artifact not trained on `rent:vallejo_ca`). The calibration
    # tab renders these distinctly; the offline deploy gate (`run_sample_sanity`) treats them
    # as failures so a misconfigured spec can't ship silently.
    status: Literal["pass", "fail", "skipped", "unmodeled"]
    detail: str  # "" when pass; failure/skip/unmodeled explanation otherwise


def run_sample_sanity_file(path: Path) -> None:
    spec = SampleSanitySpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    run_sample_sanity(spec, base_dir=path.parent)


def run_sample_sanity(spec: SampleSanitySpec, *, base_dir: Path) -> None:
    """Deploy gate: sample the model and raise if any sanity band fails or is unmodeled.

    `unmodeled` rows are deploy-blockers: the spec is documenting bands for a series the
    preset can't emit, so something is misconfigured. The calibration tab is more lenient
    (renders the band as "not modeled by <preset>" instead of 400-ing the page)."""

    results = evaluate_sample_sanity(spec, base_dir=base_dir)
    blockers = [result for result in results if result.status in {"fail", "unmodeled"}]
    if blockers:
        raise AssertionError("\n".join(f"{blocker.label}: {blocker.detail}" for blocker in blockers))


def evaluate_sample_sanity(spec: SampleSanitySpec, *, base_dir: Path) -> list[SanityBandResult]:
    """Sample the spec's provider model and evaluate every sanity band against it."""

    provider_config_path = _resolve_path(spec.provider_config_path, base_dir=base_dir)
    provider = _load_provider_config(provider_config_path)
    model = provider.realize_model()
    sampled, unmodeled_level_keys, unmodeled_pe_issuers = sample_for_spec(spec, model)
    return evaluate_sample_checks(
        spec,
        sampled,
        rollout_count=spec.rollout_count,
        horizon_months=spec.horizon_months,
        unmodeled_level_keys=unmodeled_level_keys,
        unmodeled_pe_issuers=unmodeled_pe_issuers,
    )


def partition_spec_coverage(
    spec: SampleSanitySpec, model: Sampler
) -> tuple[frozenset[LevelSeriesKey], frozenset[IssuerId], frozenset[LevelSeriesKey], frozenset[IssuerId]]:
    """Partition a spec's attempted keys into (modeled_level, modeled_pe, unmodeled_level, unmodeled_pe).

    `modeled_*` is the subset the provider advertises it can emit and goes into the sampling
    request; `unmodeled_*` becomes `status="unmodeled"` rows in the result. The caller (server
    calibration_run or `sample_for_spec`) decides which to request — this split is pure."""

    attempted_level = spec.attempted_level_keys
    attempted_pe = spec.attempted_private_equity_issuers
    emittable_level = model.emittable_level_keys()
    emittable_pe = model.emittable_private_equity_issuers()
    modeled_level = attempted_level & emittable_level
    modeled_pe = attempted_pe & emittable_pe
    unmodeled_level = attempted_level - emittable_level
    unmodeled_pe = attempted_pe - emittable_pe
    return modeled_level, modeled_pe, unmodeled_level, unmodeled_pe


def sample_for_spec(
    spec: SampleSanitySpec, model: Sampler
) -> tuple[SampledExogenousBundle, frozenset[LevelSeriesKey], frozenset[IssuerId]]:
    """Run one sampling pass that satisfies every modeled check in `spec`, plus the unmodeled split.

    Returns the sampled bundle plus the unmodeled-level-keys / unmodeled-PE-issuers sets so the
    evaluator can emit `status="unmodeled"` rows for the deferred checks. The bundle is asserted
    against the modeled subset of the request (so a provider bug — emitting nothing for a series
    it advertises — still raises, while a not-modeled series merely surfaces in the UI)."""

    modeled_level, modeled_pe, unmodeled_level, unmodeled_pe = partition_spec_coverage(spec, model)
    request = ExogenousSamplingRequest(
        horizon_months=spec.horizon_months,
        rollout_seeds=spec.rollout_seeds,
        **level_series_request_channels(modeled_level),
        required_private_equity_issuers=modeled_pe,
    )
    sampled = model.sample(request)
    validate_sample_satisfies_request(request, sampled)
    return sampled, unmodeled_level, unmodeled_pe


def evaluate_sample_checks(
    spec: SampleSanitySpec,
    sampled: SampledExogenousBundle,
    *,
    rollout_count: int,
    horizon_months: int,
    unmodeled_level_keys: frozenset[LevelSeriesKey] = frozenset(),
    unmodeled_pe_issuers: frozenset[IssuerId] = frozenset(),
) -> list[SanityBandResult]:
    """Evaluate every check in `spec` against an already-sampled bundle.

    Pure: uses the passed-in `rollout_count`/`horizon_months` (the actual sampled
    dimensions), not `spec.rollout_count`/`spec.horizon_months`, so callers reusing
    rollouts sampled at a different count/horizon get correct indexing. Any check
    whose month exceeds `horizon_months` yields a `status="skipped"` result rather
    than indexing out of bounds.

    Checks whose series/issuer is in the `unmodeled_*` partition collapse to a single
    `status="unmodeled"` summary row instead of running their bands — there's nothing in
    the bundle to evaluate against, and one row per unmodeled check keeps the calibration
    page legible (compare to the 5-10 per-band rows a fully-modeled check produces).
    """

    results: list[SanityBandResult] = []
    for level_check in spec.level_checks:
        if level_check.key in unmodeled_level_keys:
            results.append(_unmodeled_level_row(level_check))
            continue
        results.extend(
            _evaluate_level_check(level_check, sampled, rollout_count=rollout_count, horizon_months=horizon_months)
        )
    for event_kind_check in spec.event_kind_observed_checks:
        if event_kind_check.issuer_id in unmodeled_pe_issuers:
            results.append(_unmodeled_pe_row(event_kind_check.issuer_id, kind_label="event_kind"))
            continue
        results.append(
            _evaluate_event_kind_check(
                event_kind_check, sampled, rollout_count=rollout_count, horizon_months=horizon_months
            )
        )
    for mark_check in spec.private_equity_mark_checks:
        if mark_check.issuer_id in unmodeled_pe_issuers:
            results.append(_unmodeled_pe_row(mark_check.issuer_id, kind_label="mark"))
            continue
        results.extend(
            _evaluate_mark_check(mark_check, sampled, rollout_count=rollout_count, horizon_months=horizon_months)
        )
    for event_check in spec.event_checks:
        if event_check.issuer_id in unmodeled_pe_issuers:
            results.append(_unmodeled_pe_row(event_check.issuer_id, kind_label="event"))
            continue
        results.extend(
            _evaluate_event_check(event_check, sampled, rollout_count=rollout_count, horizon_months=horizon_months)
        )
    for protocol_check in spec.private_equity_protocol_checks:
        if protocol_check.issuer_id in unmodeled_pe_issuers:
            results.append(_unmodeled_pe_row(protocol_check.issuer_id, kind_label="protocol"))
            continue
        results.extend(
            _evaluate_protocol_check(
                protocol_check, sampled, rollout_count=rollout_count, horizon_months=horizon_months
            )
        )
    return results


def _unmodeled_level_row(level_check: LevelSeriesSanityCheck) -> SanityBandResult:
    series_id = level_check.key.wire_id
    return SanityBandResult(
        label=f"{series_id} (not modeled)",
        series_id=series_id,
        kind="unmodeled",
        month=None,
        expected_lower=None,
        expected_upper=None,
        observed=(),
        observed_labels=(),
        status="unmodeled",
        detail=f"level series {series_id!r} is not produced by the deployment's preset",
    )


def _unmodeled_pe_row(issuer_id: IssuerId, *, kind_label: str) -> SanityBandResult:
    series_id = f"PE issuer {issuer_id!r} {kind_label}"
    return SanityBandResult(
        label=f"{series_id} (not modeled)",
        series_id=series_id,
        kind="unmodeled",
        month=None,
        expected_lower=None,
        expected_upper=None,
        observed=(),
        observed_labels=(),
        status="unmodeled",
        detail=f"PE issuer {issuer_id!r} is not produced by the deployment's preset",
    )


def _evaluate_level_check(
    level_check: LevelSeriesSanityCheck, sampled: SampledExogenousBundle, *, rollout_count: int, horizon_months: int
) -> list[SanityBandResult]:
    series_id = level_check.key.wire_id
    levels = sampled.level_matrix(level_check.key, rollout_count=rollout_count, horizon_months=horizon_months)
    # Finiteness / positivity are correctness invariants, not reasonableness bands: a NaN/inf or
    # non-positive level/mark is a model bug, not a tunable expectation. Enforce them as hard
    # runtime assertions (raise) rather than surfacing soft pass/fail rows on the calibration page.
    _assert_finite(levels, series_id=series_id)
    if level_check.require_positive:
        _assert_positive(levels, series_id=series_id)
    results: list[SanityBandResult] = []
    if level_check.initial_value is not None:
        results.append(
            _check_anchor(
                levels,
                series_id=series_id,
                initial_value=level_check.initial_value,
                atol=level_check.initial_atol,
                rtol=level_check.initial_rtol,
                rollout_count=rollout_count,
            )
        )
    for value_bound in level_check.value_percentile_bounds:
        label = f"{series_id} value m{value_bound.month}"
        if value_bound.month > horizon_months:
            results.append(
                _skip_percentile_bound(
                    value_bound,
                    series_id=series_id,
                    label=label,
                    month=value_bound.month,
                    horizon_months=horizon_months,
                )
            )
            continue
        results.append(
            _check_percentile_bound(levels[:, value_bound.month], value_bound, series_id=series_id, label=label)
        )
    for value_range in level_check.value_percentile_ranges:
        label = f"{series_id} value m{value_range.month}"
        if value_range.month > horizon_months:
            results.append(
                _skip_percentile_range_bound(
                    value_range,
                    series_id=series_id,
                    label=label,
                    month=value_range.month,
                    horizon_months=horizon_months,
                )
            )
            continue
        results.append(
            _check_percentile_range_bound(levels[:, value_range.month], value_range, series_id=series_id, label=label)
        )
    for ratio_bound in level_check.ratio_percentile_bounds:
        label = f"{series_id} ratio m{ratio_bound.month}/m0"
        if ratio_bound.month > horizon_months:
            results.append(
                _skip_percentile_bound(
                    ratio_bound,
                    series_id=series_id,
                    label=label,
                    month=ratio_bound.month,
                    horizon_months=horizon_months,
                )
            )
            continue
        ratios = levels[:, ratio_bound.month] / levels[:, 0]
        results.append(_check_percentile_bound(ratios, ratio_bound, series_id=series_id, label=label))
    for ratio_range in level_check.ratio_percentile_ranges:
        label = f"{series_id} ratio m{ratio_range.month}/m0"
        if ratio_range.month > horizon_months:
            results.append(
                _skip_percentile_range_bound(
                    ratio_range,
                    series_id=series_id,
                    label=label,
                    month=ratio_range.month,
                    horizon_months=horizon_months,
                )
            )
            continue
        ratios = levels[:, ratio_range.month] / levels[:, 0]
        results.append(_check_percentile_range_bound(ratios, ratio_range, series_id=series_id, label=label))
    for threshold_bound in level_check.threshold_probability_bounds:
        if threshold_bound.month > horizon_months:
            results.append(
                _skip_threshold_probability_bound(threshold_bound, series_id=series_id, horizon_months=horizon_months)
            )
            continue
        results.append(
            _check_threshold_probability_bound(
                levels, threshold_bound, series_id=series_id, rollout_count=rollout_count
            )
        )
    return results


def _evaluate_mark_check(
    mark_check: PrivateEquityMarkSanityCheck,
    sampled: SampledExogenousBundle,
    *,
    rollout_count: int,
    horizon_months: int,
) -> list[SanityBandResult]:
    series_id = f"PE issuer {mark_check.issuer_id!r} mark"
    marks = sampled.private_equity.issuer_float_matrix(
        mark_check.issuer_id, "mark_usd_per_unit", rollout_count=rollout_count, horizon_months=horizon_months
    )
    # See `_evaluate_level_check`: finiteness/positivity are hard invariants, asserted not banded.
    _assert_finite(marks, series_id=series_id)
    if mark_check.require_positive:
        _assert_positive(marks, series_id=series_id)
    results: list[SanityBandResult] = []
    if mark_check.initial_value is not None:
        results.append(
            _check_anchor(
                marks,
                series_id=series_id,
                initial_value=mark_check.initial_value,
                atol=mark_check.initial_atol,
                rtol=mark_check.initial_rtol,
                rollout_count=rollout_count,
            )
        )
    for ratio_bound in mark_check.ratio_percentile_bounds:
        label = f"{series_id} ratio m{ratio_bound.month}/m0"
        if ratio_bound.month > horizon_months:
            results.append(
                _skip_percentile_bound(
                    ratio_bound,
                    series_id=series_id,
                    label=label,
                    month=ratio_bound.month,
                    horizon_months=horizon_months,
                )
            )
            continue
        ratios = marks[:, ratio_bound.month] / marks[:, 0]
        results.append(_check_percentile_bound(ratios, ratio_bound, series_id=series_id, label=label))
    for ratio_range in mark_check.ratio_percentile_ranges:
        label = f"{series_id} ratio m{ratio_range.month}/m0"
        if ratio_range.month > horizon_months:
            results.append(
                _skip_percentile_range_bound(
                    ratio_range,
                    series_id=series_id,
                    label=label,
                    month=ratio_range.month,
                    horizon_months=horizon_months,
                )
            )
            continue
        ratios = marks[:, ratio_range.month] / marks[:, 0]
        results.append(_check_percentile_range_bound(ratios, ratio_range, series_id=series_id, label=label))
    for threshold_bound in mark_check.threshold_probability_bounds:
        if threshold_bound.month > horizon_months:
            results.append(
                _skip_threshold_probability_bound(threshold_bound, series_id=series_id, horizon_months=horizon_months)
            )
            continue
        results.append(
            _check_threshold_probability_bound(marks, threshold_bound, series_id=series_id, rollout_count=rollout_count)
        )
    return results


def _event_kind_phrase(count_op: str, kind_names: str) -> str:
    if count_op == "exactly_zero":
        return f"no {kind_names}"
    return kind_names


def _evaluate_event_kind_check(
    event_kind_check: EventKindObservedCheck,
    sampled: SampledExogenousBundle,
    *,
    rollout_count: int,
    horizon_months: int,
) -> SanityBandResult:
    event_kind_codes = sampled.private_equity.issuer_int_matrix(
        event_kind_check.issuer_id, "event_kind_code", rollout_count=rollout_count, horizon_months=horizon_months
    )
    if event_kind_check.by_month > horizon_months:
        return _skip_event_kind_observed(event_kind_check, horizon_months=horizon_months)
    return _check_event_kind_observed(event_kind_codes, event_kind_check)


def _evaluate_event_check(
    event_check: EventSeriesSanityCheck, sampled: SampledExogenousBundle, *, rollout_count: int, horizon_months: int
) -> list[SanityBandResult]:
    series_id = f"PE issuer {event_check.issuer_id!r} sale_opportunity_active"
    events = sampled.private_equity.issuer_bool_matrix(
        event_check.issuer_id, "sale_opportunity_active", rollout_count=rollout_count, horizon_months=horizon_months
    )
    active_counts = events.astype(np.int64).sum(axis=1)
    results: list[SanityBandResult] = []
    for active_count_bound in event_check.active_count_percentile_bounds:
        value = float(np.percentile(active_counts, active_count_bound.percentile))
        results.append(
            _bound_result(
                value,
                lower=active_count_bound.lower,
                upper=active_count_bound.upper,
                kind="count_range",
                series_id=series_id,
                month=None,
                label=f"{event_check.issuer_id} active-count p{active_count_bound.percentile:g}",
                observed_label=f"p{active_count_bound.percentile:g}",
            )
        )
    results.extend(
        _check_percentile_count_range_bound(
            active_counts, active_count_range, series_id=series_id, label=f"{event_check.issuer_id} active-count"
        )
        for active_count_range in event_check.active_count_percentile_ranges
    )
    return results


def _evaluate_protocol_check(
    protocol_check: PrivateEquityProtocolSanityCheck,
    sampled: SampledExogenousBundle,
    *,
    rollout_count: int,
    horizon_months: int,
) -> list[SanityBandResult]:
    results: list[SanityBandResult] = []
    if protocol_check.allowed_regime_codes:
        regime_codes = sampled.private_equity.issuer_int_matrix(
            protocol_check.issuer_id, "regime_code", rollout_count=rollout_count, horizon_months=horizon_months
        )
        results.append(
            _check_codes_allowed(
                regime_codes,
                allowed=frozenset(protocol_check.allowed_regime_codes),
                series_id=f"private-equity issuer {protocol_check.issuer_id!r} regime_code",
            )
        )
    if protocol_check.allowed_event_kind_codes:
        event_kind_codes = sampled.private_equity.issuer_int_matrix(
            protocol_check.issuer_id, "event_kind_code", rollout_count=rollout_count, horizon_months=horizon_months
        )
        results.append(
            _check_codes_allowed(
                event_kind_codes,
                allowed=frozenset(protocol_check.allowed_event_kind_codes),
                series_id=f"private-equity issuer {protocol_check.issuer_id!r} event_kind_code",
            )
        )
    return results


def _load_provider_config(path: Path) -> ProviderConfig:
    raw = resolve_provider_includes(yaml.safe_load(path.read_text(encoding="utf-8")), base_dir=path.parent)
    provider = _ADAPTER.validate_python(raw)
    return _anchor_provider_paths(provider, base_dir=path.parent)


def _anchor_provider_paths(provider: ProviderConfig, *, base_dir: Path) -> ProviderConfig:
    if isinstance(provider, TrainedPrivateEquityProviderConfig):
        trained_model_path = _resolve_path(provider.trained_model_path, base_dir=base_dir)
        return provider.model_copy(update={"trained_model_path": trained_model_path})
    if isinstance(provider, StateSpaceProviderConfig):
        trained_artifact_path = _resolve_path(provider.trained_artifact_path, base_dir=base_dir)
        return provider.model_copy(update={"trained_artifact_path": trained_artifact_path})
    if isinstance(provider, VecmProviderConfig):
        trained_blob = (
            None if provider.trained_blob is None else _resolve_path(provider.trained_blob, base_dir=base_dir)
        )
        return provider.model_copy(update={"trained_blob": trained_blob})
    if isinstance(provider, MirroringProviderConfig):
        return provider.model_copy(update={"model": _anchor_provider_paths(provider.model, base_dir=base_dir)})
    if isinstance(provider, CompositeProviderConfig):
        return provider.model_copy(
            update={
                "macro": _anchor_provider_paths(provider.macro, base_dir=base_dir),
                "private_equity": _anchor_provider_paths(provider.private_equity, base_dir=base_dir),
            }
        )
    return provider


def _resolve_path(path: Path, *, base_dir: Path) -> Path:
    runfile_prefix = "runfile:"
    path_text = str(path)
    if path_text.startswith(runfile_prefix):
        return get_required_path(path_text.removeprefix(runfile_prefix))
    return path if path.is_absolute() else (base_dir / path).resolve()


def _assert_finite(values: np.ndarray, *, series_id: str) -> None:
    """Hard invariant: every sampled value must be finite. A NaN/inf is a model bug, not a
    reasonableness question, so raise rather than emit a soft band."""
    if not bool(np.all(np.isfinite(values))):
        raise AssertionError(f"{series_id} produced non-finite value(s) — this is a model bug, not a band miss")


def _assert_positive(values: np.ndarray, *, series_id: str) -> None:
    """Hard invariant: every sampled level/mark must be strictly positive (these are USD prices /
    index levels). A non-positive value is a model bug, so raise rather than emit a soft band."""
    if bool(np.any(values <= 0.0)):
        raise AssertionError(f"{series_id} produced non-positive value(s) — this is a model bug, not a band miss")


def _check_anchor(
    values: np.ndarray, *, series_id: str, initial_value: float, atol: float, rtol: float, rollout_count: int
) -> SanityBandResult:
    expected = np.full(rollout_count, float(initial_value), dtype=np.float64)
    close = bool(np.allclose(values[:, 0], expected, atol=atol, rtol=rtol))
    detail = (
        ""
        if close
        else f"{series_id} month-0 anchor mismatch: expected {float(initial_value):g} (atol={atol:g}, rtol={rtol:g})"
    )
    return SanityBandResult(
        label=f"{series_id} m0 anchor",
        series_id=series_id,
        kind="anchor",
        month=0,
        expected_lower=float(initial_value),
        expected_upper=float(initial_value),
        observed=(float(values[:, 0].min()), float(values[:, 0].max())),
        observed_labels=("m0 min", "m0 max"),
        status="pass" if close else "fail",
        detail=detail,
    )


def _check_codes_allowed(values: np.ndarray, *, allowed: frozenset[int], series_id: str) -> SanityBandResult:
    observed = frozenset(int(value) for value in np.unique(values))
    unexpected = sorted(observed - allowed)
    return SanityBandResult(
        label=f"{series_id} codes",
        series_id=series_id,
        kind="codes_allowed",
        month=None,
        expected_lower=None,
        expected_upper=None,
        observed=(),
        observed_labels=(),
        status="pass" if not unexpected else "fail",
        detail=""
        if not unexpected
        else f"{series_id} produced unexpected code(s): {unexpected}; allowed {sorted(allowed)}",
    )


def _check_percentile_bound(
    values: np.ndarray, bound: PercentileBound, *, series_id: str, label: str
) -> SanityBandResult:
    value = float(np.percentile(values, bound.percentile))
    return _bound_result(
        value,
        lower=bound.lower,
        upper=bound.upper,
        kind="percentile_bound",
        series_id=series_id,
        month=bound.month,
        label=f"{label} p{bound.percentile:g}",
        observed_label=f"p{bound.percentile:g}",
    )


def _check_percentile_range_bound(
    values: np.ndarray, bound: PercentileRangeBound, *, series_id: str, label: str
) -> SanityBandResult:
    lower_value = float(np.percentile(values, bound.lower_percentile))
    upper_value = float(np.percentile(values, bound.upper_percentile))
    return _range_result(
        lower_value,
        upper_value,
        lower=bound.lower,
        upper=bound.upper,
        series_id=series_id,
        month=bound.month,
        label=f"{label} p{bound.lower_percentile:g}..p{bound.upper_percentile:g}",
        observed_labels=(f"p{bound.lower_percentile:g}", f"p{bound.upper_percentile:g}"),
    )


def _check_percentile_count_range_bound(
    values: np.ndarray, bound: EventCountPercentileRangeBound, *, series_id: str, label: str
) -> SanityBandResult:
    lower_value = float(np.percentile(values, bound.lower_percentile))
    upper_value = float(np.percentile(values, bound.upper_percentile))
    return _range_result(
        lower_value,
        upper_value,
        lower=bound.lower,
        upper=bound.upper,
        series_id=series_id,
        month=None,
        label=f"{label} p{bound.lower_percentile:g}..p{bound.upper_percentile:g}",
        observed_labels=(f"p{bound.lower_percentile:g}", f"p{bound.upper_percentile:g}"),
        kind="count_range",
    )


_LEVEL_COMPARATORS = {"lt": np.less, "le": np.less_equal, "gt": np.greater, "ge": np.greater_equal}


def _check_threshold_probability_bound(
    levels: np.ndarray, bound: LevelThresholdProbabilityBound, *, series_id: str, rollout_count: int
) -> SanityBandResult:
    if bound.threshold_kind == "absolute":
        threshold = np.full(rollout_count, bound.threshold, dtype=np.float64)
    else:
        threshold = bound.threshold * levels[:, 0]
    comparator = _LEVEL_COMPARATORS[bound.comparison]
    successes = comparator(levels[:, bound.month], threshold)
    probability = float(successes.mean())
    label = (
        f"{series_id} P(level {bound.comparison} {bound.threshold:g}"
        f"{' * initial' if bound.threshold_kind == 'ratio_of_initial' else ''} at m{bound.month})"
    )
    return _bound_result(
        probability,
        lower=bound.probability_lower,
        upper=bound.probability_upper,
        kind="threshold_probability",
        series_id=series_id,
        month=bound.month,
        label=label,
        observed_label="probability",
    )


def _check_event_kind_observed(event_kind_codes: np.ndarray, bound: EventKindObservedCheck) -> SanityBandResult:
    window = event_kind_codes[:, : bound.by_month + 1]
    occurrence_mask = np.isin(window, np.asarray(bound.event_kind_codes, dtype=window.dtype))
    occurs_per_rollout = occurrence_mask.any(axis=1)
    successes = occurs_per_rollout if bound.count_op == "at_least_one" else ~occurs_per_rollout
    probability = float(successes.mean())
    kind_names = ",".join(PrivateEquityEventKindCode(code).name for code in bound.event_kind_codes)
    series_id = f"private-equity issuer {bound.issuer_id!r} event_kind_code"
    event_phrase = _event_kind_phrase(bound.count_op, kind_names)
    label = f"private-equity issuer {bound.issuer_id!r} P({event_phrase} by m{bound.by_month})"
    return _bound_result(
        probability,
        lower=bound.probability_lower,
        upper=bound.probability_upper,
        kind="event_kind_probability",
        series_id=series_id,
        month=bound.by_month,
        label=label,
        observed_label="probability",
    )


def _bound_result(
    value: float,
    *,
    lower: float | None,
    upper: float | None,
    kind: str,
    series_id: str,
    month: int | None,
    label: str,
    observed_label: str,
) -> SanityBandResult:
    if lower is not None and value < lower:
        detail = f"{value:g} is below lower bound {lower:g}"
    elif upper is not None and value > upper:
        detail = f"{value:g} is above upper bound {upper:g}"
    else:
        detail = ""
    return SanityBandResult(
        label=label,
        series_id=series_id,
        kind=kind,
        month=month,
        expected_lower=lower,
        expected_upper=upper,
        observed=(value,),
        observed_labels=(observed_label,),
        status="pass" if not detail else "fail",
        detail=detail,
    )


def _range_result(
    lower_value: float,
    upper_value: float,
    *,
    lower: float,
    upper: float,
    series_id: str,
    month: int | None,
    label: str,
    observed_labels: tuple[str, str],
    kind: str = "percentile_range",
) -> SanityBandResult:
    out_of_range = lower_value < lower or upper_value > upper
    detail = (
        ""
        if not out_of_range
        else f"[{lower_value:g}, {upper_value:g}] is outside expected range [{lower:g}, {upper:g}]"
    )
    return SanityBandResult(
        label=label,
        series_id=series_id,
        kind=kind,
        month=month,
        expected_lower=lower,
        expected_upper=upper,
        observed=(lower_value, upper_value),
        observed_labels=observed_labels,
        status="pass" if not out_of_range else "fail",
        detail=detail,
    )


def _skip_percentile_bound(
    bound: PercentileBound, *, series_id: str, label: str, month: int, horizon_months: int
) -> SanityBandResult:
    return SanityBandResult(
        label=f"{label} p{bound.percentile:g}",
        series_id=series_id,
        kind="percentile_bound",
        month=month,
        expected_lower=bound.lower,
        expected_upper=bound.upper,
        observed=(),
        observed_labels=(),
        status="skipped",
        detail=f"month {month} > sampled horizon {horizon_months}",
    )


def _skip_percentile_range_bound(
    bound: PercentileRangeBound, *, series_id: str, label: str, month: int, horizon_months: int
) -> SanityBandResult:
    return SanityBandResult(
        label=f"{label} p{bound.lower_percentile:g}..p{bound.upper_percentile:g}",
        series_id=series_id,
        kind="percentile_range",
        month=month,
        expected_lower=bound.lower,
        expected_upper=bound.upper,
        observed=(),
        observed_labels=(),
        status="skipped",
        detail=f"month {month} > sampled horizon {horizon_months}",
    )


def _skip_threshold_probability_bound(
    bound: LevelThresholdProbabilityBound, *, series_id: str, horizon_months: int
) -> SanityBandResult:
    label = (
        f"{series_id} P(level {bound.comparison} {bound.threshold:g}"
        f"{' * initial' if bound.threshold_kind == 'ratio_of_initial' else ''} at m{bound.month})"
    )
    return SanityBandResult(
        label=label,
        series_id=series_id,
        kind="threshold_probability",
        month=bound.month,
        expected_lower=bound.probability_lower,
        expected_upper=bound.probability_upper,
        observed=(),
        observed_labels=(),
        status="skipped",
        detail=f"month {bound.month} > sampled horizon {horizon_months}",
    )


def _skip_event_kind_observed(bound: EventKindObservedCheck, *, horizon_months: int) -> SanityBandResult:
    kind_names = ",".join(PrivateEquityEventKindCode(code).name for code in bound.event_kind_codes)
    series_id = f"private-equity issuer {bound.issuer_id!r} event_kind_code"
    event_phrase = _event_kind_phrase(bound.count_op, kind_names)
    label = f"private-equity issuer {bound.issuer_id!r} P({event_phrase} by m{bound.by_month})"
    return SanityBandResult(
        label=label,
        series_id=series_id,
        kind="event_kind_probability",
        month=bound.by_month,
        expected_lower=bound.probability_lower,
        expected_upper=bound.probability_upper,
        observed=(),
        observed_labels=(),
        status="skipped",
        detail=f"month {bound.by_month} > sampled horizon {horizon_months}",
    )
