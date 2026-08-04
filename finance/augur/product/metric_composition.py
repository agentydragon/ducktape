"""How the derived product metrics are built out of the base ones. The only place.

Two things compute product metrics, for a real reason: the engine reduces them on-device
straight out of the scan carry (so a single-metric fan never materializes nine histories),
while `product/decode.py` reads them back out of the dense buffers. Those are genuinely
different jobs.

The *arithmetic relating the metrics to each other* is not. Net worth is the same sum
either way, and having it written twice meant a new asset class had to be added to both --
which is how it was noticed. A test asserting the two agree is not a fix for that; it only
reports the drift after someone causes it.

So the base series stay per-backend and the composition lives here, written once against
whatever `base` returns. `numpy` arrays, `jnp` arrays and plain floats all support the
operators used, so one definition serves every caller.

`base` is a callable rather than a mapping to keep the engine's laziness: only the base
series a requested metric actually needs get materialized.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, Self

# What the simulation emits directly, before any metric is derived from another.
BASE_METRIC_NAMES = (
    "cash_usd",
    "holding_value_usd",
    "private_equity_value_usd",
    "property_value_usd",
    "mortgage_balance_usd",
    "shortfall_usd",
    "bond_value_usd",
)

DERIVED_METRIC_NAMES = ("home_equity_usd", "liquid_net_worth_usd", "net_worth_usd")

METRIC_NAMES = (*BASE_METRIC_NAMES, *DERIVED_METRIC_NAMES)


class MetricValue(Protocol):
    """What a metric series has to support to be composed: adding and subtracting its own
    kind. `numpy` arrays, `jnp` arrays and plain floats all qualify, which is the point —
    the composition is written once and each backend supplies its own base series.
    """

    def __add__(self, other: Self, /) -> Self: ...
    def __sub__(self, other: Self, /) -> Self: ...


def compose_metric[T: MetricValue](name: str, base: Callable[[str], T]) -> T:
    """One product metric, in terms of the base series `base` supplies.

    A base metric is returned as-is; a derived one is its defining sum. Raises on an unknown
    name rather than falling through, so a typo is not silently a missing series.
    """

    match name:
        case "home_equity_usd":
            return base("property_value_usd") - base("mortgage_balance_usd")
        case "liquid_net_worth_usd":
            # Excludes private equity AND bonds by design. PE is saleable only at sparse
            # tender events; a bond held to maturity is never marked and never sold. Neither
            # is "cash you could get tomorrow", which is what this metric means — and the
            # PrivateEquityTenderPolicy floor reads it with exactly that meaning.
            return base("cash_usd") + base("holding_value_usd")
        case "net_worth_usd":
            return (
                compose_metric("liquid_net_worth_usd", base)
                + compose_metric("home_equity_usd", base)
                + base("private_equity_value_usd")
                + base("bond_value_usd")
            )
        case _ if name in BASE_METRIC_NAMES:
            return base(name)
    raise ValueError(f"unknown product metric {name!r}; known: {', '.join(METRIC_NAMES)}")
