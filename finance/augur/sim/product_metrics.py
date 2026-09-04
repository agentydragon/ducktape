"""The product read model's metric types, shared by every simulation backend.

These carry no backend detail: a backend supplies the seven base series and the failure
vector, and everything above that — the derived metrics, the percentile fan, the terminal
distribution — is composed here, once, from `product.metric_composition`. That is what
lets JAX and Rust hand the product API the same objects rather than two lookalikes that
have to be kept in agreement.
"""

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass

import numpy as np
from jaxtyping import Int64

from finance.augur.product.metric_composition import BASE_METRIC_NAMES, DERIVED_METRIC_NAMES, compose_metric


@dataclass(frozen=True)
class ProductMetricFanSummary:
    """Exact percentile reductions for one product metric."""

    month_index: Int64[np.ndarray, " snapshot"]
    failed_count: int
    currency_code: str
    currency_quantum: str
    percentiles: tuple[float, ...]
    terminal_percentiles: Int64[np.ndarray, " percentile"]
    monthly_percentiles: Int64[np.ndarray, " snapshot percentile"]


@dataclass(frozen=True)
class ProductTerminalSummary:
    """Per-rollout terminal samples for one product metric."""

    failed_month: Int64[np.ndarray, " rollout"]
    currency_code: str
    currency_quantum: str
    terminal_samples: Int64[np.ndarray, " rollout"]


@dataclass(frozen=True)
class ProductProjectionSummaries:
    """Metric-fan and terminal-distribution summaries from one product scan."""

    metric_fan: ProductMetricFanSummary
    terminal_distribution: ProductTerminalSummary


@dataclass(frozen=True)
class ProductMetricArrays:
    """The base product series for a whole population, plus its failure vector."""

    month_index: Int64[np.ndarray, " snapshot"]
    failed_month: Int64[np.ndarray, " rollout"]
    currency_code: str
    currency_quantum: str
    base_series: tuple[Int64[np.ndarray, " snapshot rollout"], ...]

    def metric_arrays(self) -> dict[str, Int64[np.ndarray, " snapshot rollout"]]:
        base = dict(zip(BASE_METRIC_NAMES, self.base_series, strict=True))
        return {
            "month_index": self.month_index,
            **base,
            **{name: compose_metric(name, base.__getitem__) for name in DERIVED_METRIC_NAMES},
        }
