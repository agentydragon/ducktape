"""The product read model's metric types, shared by every simulation backend.

These carry no backend detail: a backend supplies the seven base series and the failure
vector, and everything above that — the derived metrics, the percentile fan, the terminal
distribution — is composed here, once, from `sim.metric_composition`. An engine
therefore owes the product API these objects and not a read model of its own.
"""

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from jaxtyping import Bool, Int64

from finance.augur.sim.metric_composition import BASE_METRIC_NAMES, DERIVED_METRIC_NAMES, compose_metric


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

    month_index: Int64[np.ndarray, " snapshot"]
    failed_month: Int64[np.ndarray, " rollout"]
    currency_code: str
    currency_quantum: str
    base_series: tuple[Int64[np.ndarray, " snapshot rollout"], ...]

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
