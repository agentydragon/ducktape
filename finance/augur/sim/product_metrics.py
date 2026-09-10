"""Product metric arrays and pure reductions, independent of trajectory execution."""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from jaxtyping import Bool, Int64

from finance.augur.sim.metric_composition import (
    BASE_METRIC_NAMES,
    DERIVED_METRIC_NAMES,
    compose_metric,
    terminal_series,
)
from finance.augur.sim.quantiles import currency_quantiles


class OutcomeBasis(StrEnum):
    """Aggregate population; historical monthly fans have their own observation counts."""

    COMPLETED_HORIZON = "completed_horizon"
    OBSERVED_THROUGH_STOP = "observed_through_stop"


@dataclass(frozen=True)
class ProductMetricFanSummary:
    """Exact percentile reductions for one product metric."""

    month_index: Int64[np.ndarray, " snapshot"]
    basis: OutcomeBasis
    failed_count: int
    currency_code: str
    currency_quantum: str
    percentiles: tuple[float, ...]
    terminal_percentiles: Int64[np.ndarray, " percentile"] | None
    monthly_percentiles: Int64[np.ndarray, " snapshot percentile"]
    # A zero count makes that month's integer storage unobserved, not a zero quantile.
    observed_count: Int64[np.ndarray, " snapshot"]


@dataclass(frozen=True)
class ProductTerminalSummary:
    """Exact outcome samples, with an explicit observation basis and validity."""

    basis: OutcomeBasis
    failed_month: Int64[np.ndarray, " rollout"]
    currency_code: str
    currency_quantum: str
    terminal_samples: Int64[np.ndarray, " rollout"]

    @property
    def observed(self) -> Bool[np.ndarray, " rollout"]:
        return (
            np.ones(self.failed_month.shape, dtype=bool)
            if self.basis == OutcomeBasis.OBSERVED_THROUGH_STOP
            else self.failed_month < 0
        )


@dataclass(frozen=True)
class ProductProjectionSummaries:
    """Metric-fan and terminal-distribution summaries from one product scan."""

    metric_fan: ProductMetricFanSummary
    terminal_distribution: ProductTerminalSummary


@dataclass(frozen=True)
class ProductMetricArrays:
    """Exact integer blocks plus observation validity; masked storage is not money."""

    rollout_ids: tuple[int, ...]
    month_index: Int64[np.ndarray, " snapshot"]
    failed_month: Int64[np.ndarray, " rollout"]
    currency_code: str
    currency_quantum: str
    base_series: tuple[Int64[np.ndarray, " snapshot rollout"], ...]

    def __post_init__(self) -> None:
        if len(set(self.rollout_ids)) != len(self.rollout_ids) or any(id_ < 0 for id_ in self.rollout_ids):
            raise ValueError("metric rollout IDs must be unique and nonnegative")
        if self.failed_month.shape != (len(self.rollout_ids),):
            raise ValueError("failure vector must have one entry per rollout ID")
        if len(self.base_series) != len(BASE_METRIC_NAMES) or any(
            values.shape != (len(self.month_index), len(self.rollout_ids)) for values in self.base_series
        ):
            raise ValueError("base metric arrays must align with the snapshot and rollout ID axes")

    def select(self, rollout_ids: tuple[int, ...]) -> ProductMetricArrays:
        """Select/reorder original IDs without detaching labels from array columns."""
        missing = set(rollout_ids) - set(self.rollout_ids)
        if missing:
            raise ValueError(f"unknown metric rollout IDs: {sorted(missing)}")
        columns = [self.rollout_ids.index(id_) for id_ in rollout_ids]
        return ProductMetricArrays(
            rollout_ids=rollout_ids,
            month_index=self.month_index,
            failed_month=self.failed_month[columns],
            currency_code=self.currency_code,
            currency_quantum=self.currency_quantum,
            base_series=tuple(values[:, columns] for values in self.base_series),
        )

    @property
    def observed(self) -> Bool[np.ndarray, " snapshot rollout"]:
        """Opening and post-event snapshots through stopping, including the stop book."""
        return (self.failed_month[None, :] < 0) | (self.month_index[:, None] <= self.failed_month[None, :] + 1)

    @property
    def scheduled_observed(self) -> Bool[np.ndarray, " snapshot rollout"]:
        """Scheduled marks only: a stop book has not reached its next market observation."""
        return (self.failed_month[None, :] < 0) | (self.month_index[:, None] <= self.failed_month[None, :])

    def metric_arrays(self) -> dict[str, Int64[np.ndarray, " snapshot rollout"]]:
        base = dict(zip(BASE_METRIC_NAMES, self.base_series, strict=True))
        return {
            "month_index": self.month_index,
            **base,
            **{name: compose_metric(name, base.__getitem__) for name in DERIVED_METRIC_NAMES},
        }


def _metric_series(
    arrays: ProductMetricArrays, metric: str
) -> tuple[Int64[np.ndarray, " snapshot rollout"], Int64[np.ndarray, " rollout"], OutcomeBasis]:
    base = dict(zip(BASE_METRIC_NAMES, arrays.base_series, strict=True))
    series = compose_metric(metric, base.__getitem__)
    basis = OutcomeBasis.OBSERVED_THROUGH_STOP if metric == "shortfall_quanta" else OutcomeBasis.COMPLETED_HORIZON
    return series, terminal_series(metric, series), basis


def metric_fan(arrays: ProductMetricArrays, *, metric: str, percentiles: tuple[float, ...]) -> ProductMetricFanSummary:
    series, terminal, basis = _metric_series(arrays, metric)
    observed = arrays.observed if basis == OutcomeBasis.OBSERVED_THROUGH_STOP else arrays.scheduled_observed
    observed_count = observed.sum(axis=1)
    monthly = np.zeros((series.shape[0], len(percentiles)), dtype=np.int64)
    for month, mask in enumerate(observed):
        if observed_count[month]:
            monthly[month] = currency_quantiles(series[month, mask], percentiles)
    samples = terminal if basis == OutcomeBasis.OBSERVED_THROUGH_STOP else terminal[arrays.failed_month < 0]
    return ProductMetricFanSummary(
        basis=basis,
        month_index=arrays.month_index,
        failed_count=int((arrays.failed_month >= 0).sum()),
        currency_code=arrays.currency_code,
        currency_quantum=arrays.currency_quantum,
        percentiles=percentiles,
        terminal_percentiles=np.asarray(currency_quantiles(samples, percentiles), dtype=np.int64)
        if samples.size
        else None,
        monthly_percentiles=monthly,
        observed_count=observed_count,
    )


def terminal_summary(arrays: ProductMetricArrays, *, metric: str) -> ProductTerminalSummary:
    _, terminal, basis = _metric_series(arrays, metric)
    return ProductTerminalSummary(
        basis=basis,
        failed_month=arrays.failed_month,
        currency_code=arrays.currency_code,
        currency_quantum=arrays.currency_quantum,
        terminal_samples=np.asarray(terminal, dtype=np.int64),
    )


def projection_summaries(
    arrays: ProductMetricArrays, *, metric: str, percentiles: tuple[float, ...]
) -> ProductProjectionSummaries:
    return ProductProjectionSummaries(
        metric_fan=metric_fan(arrays, metric=metric, percentiles=percentiles),
        terminal_distribution=terminal_summary(arrays, metric=metric),
    )
