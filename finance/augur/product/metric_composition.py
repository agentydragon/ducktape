"""How the derived product metrics are built out of the base ones. The only place.

The engine emits the base series directly from the scan carry. Product fan routes use
those device arrays to select one metric, while selected-rollout detail copies all base
series after the same reducer runs. The host only composes derived sums; it never
re-values lots, properties, or bonds from dense output arrays.

The arithmetic relating metrics is defined once here. `numpy` arrays, `jnp` arrays and
plain floats all support the operators used, so one definition serves the device fan
selection and the selected-rollout host projection.

`base` is a callable rather than a mapping to keep the engine's laziness: only the base
series a requested metric actually needs get materialized.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, Self

# What the simulation emits directly, before any metric is derived from another.
BASE_METRIC_NAMES = (
    "cash_quanta",
    "holding_value_quanta",
    "private_equity_value_quanta",
    "property_value_quanta",
    "mortgage_balance_quanta",
    "shortfall_quanta",
    "bond_value_quanta",
)

DERIVED_METRIC_NAMES = ("home_equity_quanta", "liquid_net_worth_quanta", "net_worth_quanta")

METRIC_NAMES = (*BASE_METRIC_NAMES, *DERIVED_METRIC_NAMES)


class MetricValue(Protocol):
    """What a metric series has to support to be composed: adding and subtracting its own
    kind. `numpy` arrays, `jnp` arrays and plain floats all qualify, which is the point —
    the composition is written once and each backend supplies its own base series.
    """

    def __add__(self, other: Self, /) -> Self: ...
    def __sub__(self, other: Self, /) -> Self: ...


class MetricSeries(Protocol):
    """What a metric's `(snapshot, rollout)` series has to support to be reduced to its
    terminal samples. Like `MetricValue`, this is a Protocol because the two backends'
    array types — `numpy` host arrays and `jnp` device arrays — cannot be named together
    without importing JAX into the backend-neutral reduction.
    """

    def sum(self, axis: int) -> Self: ...
    def __getitem__(self, index: int, /) -> Self: ...


def terminal_series[T: MetricSeries](metric: str, series: T) -> T:
    """Terminal samples: cumulative shortfall, final snapshot for every other metric."""

    return series.sum(axis=0) if metric == "shortfall_quanta" else series[-1]


def compose_metric[T: MetricValue](name: str, base: Callable[[str], T]) -> T:
    """One product metric, in terms of the base series `base` supplies.

    A base metric is returned as-is; a derived one is its defining sum. Raises on an unknown
    name rather than falling through, so a typo is not silently a missing series.
    """

    match name:
        case "home_equity_quanta":
            return base("property_value_quanta") - base("mortgage_balance_quanta")
        case "liquid_net_worth_quanta":
            # Excludes private equity AND bonds by design. PE is saleable only at sparse
            # tender events; a bond held to maturity is never marked and never sold. Neither
            # is "cash you could get tomorrow", which is what this metric means — and the
            # PrivateEquityTenderPolicy floor reads it with exactly that meaning.
            return base("cash_quanta") + base("holding_value_quanta")
        case "net_worth_quanta":
            return (
                compose_metric("liquid_net_worth_quanta", base)
                + compose_metric("home_equity_quanta", base)
                + base("private_equity_value_quanta")
                + base("bond_value_quanta")
            )
        case _ if name in BASE_METRIC_NAMES:
            return base(name)
    raise ValueError(f"unknown product metric {name!r}; known: {', '.join(METRIC_NAMES)}")
