"""Model-agnostic sanity checks for sampled exogenous trajectories."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import yaml
from pydantic import Field, TypeAdapter, model_validator

from augur.model.exogenous import ExogenousSamplingRequest, validate_sample_satisfies_request
from augur.model.provider_config import (
    CompositeProviderConfig,
    ProviderConfig,
    StateSpaceProviderConfig,
    TrainedPrivateEquityProviderConfig,
    VecmProviderConfig,
)
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
    provider_config_path: Path
    horizon_months: int = Field(ge=0)
    rollout_seed_start: int = Field(default=1301, ge=0)
    rollout_count: int = Field(gt=0)
    required_level_series: tuple[LevelSeriesKey, ...] = ()
    required_private_equity_issuers: tuple[IssuerId, ...] = ()
    level_checks: tuple[LevelSeriesSanityCheck, ...] = ()
    event_checks: tuple[EventSeriesSanityCheck, ...] = ()
    event_kind_observed_checks: tuple[EventKindObservedCheck, ...] = ()
    private_equity_protocol_checks: tuple[PrivateEquityProtocolSanityCheck, ...] = ()
    private_equity_mark_checks: tuple[PrivateEquityMarkSanityCheck, ...] = ()

    @property
    def rollout_seeds(self) -> tuple[int, ...]:
        return tuple(range(self.rollout_seed_start, self.rollout_seed_start + self.rollout_count))


def run_sample_sanity_file(path: Path) -> None:
    spec = SampleSanitySpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    run_sample_sanity(spec, base_dir=path.parent)


def run_sample_sanity(spec: SampleSanitySpec, *, base_dir: Path) -> None:
    provider_config_path = _resolve_path(spec.provider_config_path, base_dir=base_dir)
    provider = _load_provider_config(provider_config_path)
    model = provider.realize_model()
    request = ExogenousSamplingRequest(
        horizon_months=spec.horizon_months,
        rollout_seeds=spec.rollout_seeds,
        required_level_series=frozenset(spec.required_level_series),
        required_private_equity_issuers=frozenset(spec.required_private_equity_issuers),
    )
    sampled = model.sample(request)
    validate_sample_satisfies_request(request, sampled)

    for level_check in spec.level_checks:
        levels = sampled.level_matrix(
            level_check.key, rollout_count=spec.rollout_count, horizon_months=spec.horizon_months
        )
        _assert_finite(levels, label=level_check.key.wire_id)
        if level_check.require_positive and np.any(levels <= 0.0):
            raise AssertionError(f"series {level_check.key.wire_id!r} produced non-positive level(s)")
        if level_check.initial_value is not None:
            np.testing.assert_allclose(
                levels[:, 0],
                np.full(spec.rollout_count, float(level_check.initial_value), dtype=np.float64),
                atol=level_check.initial_atol,
                rtol=level_check.initial_rtol,
                err_msg=f"series {level_check.key.wire_id!r} month-0 anchor mismatch",
            )
        for value_bound in level_check.value_percentile_bounds:
            _check_percentile_bound(
                levels[:, value_bound.month], value_bound, label=f"{level_check.key.wire_id} value m{value_bound.month}"
            )
        for value_range in level_check.value_percentile_ranges:
            _check_percentile_range_bound(
                levels[:, value_range.month], value_range, label=f"{level_check.key.wire_id} value m{value_range.month}"
            )
        for ratio_bound in level_check.ratio_percentile_bounds:
            ratios = levels[:, ratio_bound.month] / levels[:, 0]
            _check_percentile_bound(
                ratios, ratio_bound, label=f"{level_check.key.wire_id} ratio m{ratio_bound.month}/m0"
            )
        for ratio_range in level_check.ratio_percentile_ranges:
            ratios = levels[:, ratio_range.month] / levels[:, 0]
            _check_percentile_range_bound(
                ratios, ratio_range, label=f"{level_check.key.wire_id} ratio m{ratio_range.month}/m0"
            )
        for threshold_bound in level_check.threshold_probability_bounds:
            _check_threshold_probability_bound(
                levels, threshold_bound, series_id=level_check.key.wire_id, rollout_count=spec.rollout_count
            )

    for event_kind_check in spec.event_kind_observed_checks:
        event_kind_codes = sampled.private_equity.issuer_int_matrix(
            event_kind_check.issuer_id,
            "event_kind_code",
            rollout_count=spec.rollout_count,
            horizon_months=spec.horizon_months,
        )
        _check_event_kind_observed(event_kind_codes, event_kind_check)

    for mark_check in spec.private_equity_mark_checks:
        marks = sampled.private_equity.issuer_float_matrix(
            mark_check.issuer_id,
            "mark_usd_per_unit",
            rollout_count=spec.rollout_count,
            horizon_months=spec.horizon_months,
        )
        label_prefix = f"PE issuer {mark_check.issuer_id!r} mark"
        _assert_finite(marks, label=label_prefix)
        if mark_check.require_positive and np.any(marks <= 0.0):
            raise AssertionError(f"{label_prefix} produced non-positive value(s)")
        if mark_check.initial_value is not None:
            np.testing.assert_allclose(
                marks[:, 0],
                np.full(spec.rollout_count, float(mark_check.initial_value), dtype=np.float64),
                atol=mark_check.initial_atol,
                rtol=mark_check.initial_rtol,
                err_msg=f"{label_prefix} month-0 anchor mismatch",
            )
        for ratio_bound in mark_check.ratio_percentile_bounds:
            ratios = marks[:, ratio_bound.month] / marks[:, 0]
            _check_percentile_bound(ratios, ratio_bound, label=f"{label_prefix} ratio m{ratio_bound.month}/m0")
        for ratio_range in mark_check.ratio_percentile_ranges:
            ratios = marks[:, ratio_range.month] / marks[:, 0]
            _check_percentile_range_bound(ratios, ratio_range, label=f"{label_prefix} ratio m{ratio_range.month}/m0")
        for threshold_bound in mark_check.threshold_probability_bounds:
            _check_threshold_probability_bound(
                marks, threshold_bound, series_id=label_prefix, rollout_count=spec.rollout_count
            )

    for event_check in spec.event_checks:
        events = sampled.private_equity.issuer_bool_matrix(
            event_check.issuer_id,
            "sale_opportunity_active",
            rollout_count=spec.rollout_count,
            horizon_months=spec.horizon_months,
        )
        active_counts = events.astype(np.int64).sum(axis=1)
        for active_count_bound in event_check.active_count_percentile_bounds:
            value = float(np.percentile(active_counts, active_count_bound.percentile))
            _assert_bound(
                value,
                lower=active_count_bound.lower,
                upper=active_count_bound.upper,
                label=f"{event_check.issuer_id} active-count p{active_count_bound.percentile:g}",
            )
        for active_count_range in event_check.active_count_percentile_ranges:
            _check_percentile_count_range_bound(
                active_counts, active_count_range, label=f"{event_check.issuer_id} active-count"
            )

    for protocol_check in spec.private_equity_protocol_checks:
        regime_codes = sampled.private_equity.issuer_int_matrix(
            protocol_check.issuer_id,
            "regime_code",
            rollout_count=spec.rollout_count,
            horizon_months=spec.horizon_months,
        )
        event_kind_codes = sampled.private_equity.issuer_int_matrix(
            protocol_check.issuer_id,
            "event_kind_code",
            rollout_count=spec.rollout_count,
            horizon_months=spec.horizon_months,
        )
        if protocol_check.allowed_regime_codes:
            _assert_codes_allowed(
                regime_codes,
                allowed=frozenset(protocol_check.allowed_regime_codes),
                label=f"private-equity issuer {protocol_check.issuer_id!r} regime_code",
            )
        if protocol_check.allowed_event_kind_codes:
            _assert_codes_allowed(
                event_kind_codes,
                allowed=frozenset(protocol_check.allowed_event_kind_codes),
                label=f"private-equity issuer {protocol_check.issuer_id!r} event_kind_code",
            )


def _load_provider_config(path: Path) -> ProviderConfig:
    provider = _ADAPTER.validate_python(yaml.safe_load(path.read_text(encoding="utf-8")))
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


def _assert_finite(values: np.ndarray, *, label: str) -> None:
    if not np.all(np.isfinite(values)):
        raise AssertionError(f"{label} produced non-finite value(s)")


def _assert_codes_allowed(values: np.ndarray, *, allowed: frozenset[int], label: str) -> None:
    observed = frozenset(int(value) for value in np.unique(values))
    unexpected = sorted(observed - allowed)
    if unexpected:
        raise AssertionError(f"{label} produced unexpected code(s): {unexpected}; allowed {sorted(allowed)}")


def _check_percentile_bound(values: np.ndarray, bound: PercentileBound, *, label: str) -> None:
    value = float(np.percentile(values, bound.percentile))
    _assert_bound(value, lower=bound.lower, upper=bound.upper, label=f"{label} p{bound.percentile:g}")


def _check_percentile_range_bound(values: np.ndarray, bound: PercentileRangeBound, *, label: str) -> None:
    lower_value = float(np.percentile(values, bound.lower_percentile))
    upper_value = float(np.percentile(values, bound.upper_percentile))
    _assert_range_bound(
        lower_value,
        upper_value,
        lower=bound.lower,
        upper=bound.upper,
        label=f"{label} p{bound.lower_percentile:g}..p{bound.upper_percentile:g}",
    )


def _check_percentile_count_range_bound(
    values: np.ndarray, bound: EventCountPercentileRangeBound, *, label: str
) -> None:
    lower_value = float(np.percentile(values, bound.lower_percentile))
    upper_value = float(np.percentile(values, bound.upper_percentile))
    _assert_range_bound(
        lower_value,
        upper_value,
        lower=bound.lower,
        upper=bound.upper,
        label=f"{label} p{bound.lower_percentile:g}..p{bound.upper_percentile:g}",
    )


_LEVEL_COMPARATORS = {"lt": np.less, "le": np.less_equal, "gt": np.greater, "ge": np.greater_equal}


def _check_threshold_probability_bound(
    levels: np.ndarray, bound: LevelThresholdProbabilityBound, *, series_id: str, rollout_count: int
) -> None:
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
    _assert_bound(probability, lower=bound.probability_lower, upper=bound.probability_upper, label=label)


def _check_event_kind_observed(event_kind_codes: np.ndarray, bound: EventKindObservedCheck) -> None:
    window = event_kind_codes[:, : bound.by_month + 1]
    occurrence_mask = np.isin(window, np.asarray(bound.event_kind_codes, dtype=window.dtype))
    occurs_per_rollout = occurrence_mask.any(axis=1)
    successes = occurs_per_rollout if bound.count_op == "at_least_one" else ~occurs_per_rollout
    probability = float(successes.mean())
    kind_names = ",".join(PrivateEquityEventKindCode(code).name for code in bound.event_kind_codes)
    label = (
        f"private-equity issuer {bound.issuer_id!r} "
        f"P({bound.count_op.replace('_', ' ')} of {{{kind_names}}} by m{bound.by_month})"
    )
    _assert_bound(probability, lower=bound.probability_lower, upper=bound.probability_upper, label=label)


def _assert_bound(value: float, *, lower: float | None, upper: float | None, label: str) -> None:
    if lower is not None and value < lower:
        raise AssertionError(f"{label}={value:g} is below lower bound {lower:g}")
    if upper is not None and value > upper:
        raise AssertionError(f"{label}={value:g} is above upper bound {upper:g}")


def _assert_range_bound(lower_value: float, upper_value: float, *, lower: float, upper: float, label: str) -> None:
    if lower_value < lower or upper_value > upper:
        raise AssertionError(
            f"{label}=[{lower_value:g}, {upper_value:g}] is outside expected range [{lower:g}, {upper:g}]"
        )
