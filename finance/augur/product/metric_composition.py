"""How the derived product metrics are built out of the base ones. The only place.

An engine emits the base series and nothing above them: the arithmetic relating metrics
is defined once, here, and never re-derived by a caller. Nothing here re-values lots,
properties, or bonds.

`base` is a callable rather than a mapping to keep the reduction lazy: only the base
series a requested metric actually needs get materialized.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import numpy as np
from numpy.typing import NDArray

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


def terminal_series(metric: str, series: NDArray[np.int64]) -> NDArray[np.int64]:
    """Terminal samples: cumulative shortfall, final snapshot for every other metric."""

    # numpy declares both `sum` and integer indexing as returning `Any`; the dtype is the
    # input's either way.
    terminal = series.sum(axis=0) if metric == "shortfall_quanta" else series[-1]
    return cast("NDArray[np.int64]", terminal)


def compose_metric(name: str, base: Callable[[str], NDArray[np.int64]]) -> NDArray[np.int64]:
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
