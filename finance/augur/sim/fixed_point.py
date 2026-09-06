"""Exact money and fixed-point helpers for Augur.

The simulator's money contract is a currency-specific integer quantum count.
``Decimal`` is used only at an explicitly declared boundary: parsing an exact
human/API decimal or quantizing a model-owned sampled price path before that
path enters the simulator.  The engine receives and produces integer money
values only.

Keeping conversion policy here provides one auditable definition rather than
several subtly different ``round(value * 100)`` calls.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import numpy as np
from jaxtyping import Float64, Int64

from finance.augur.product.asset_key import AssetKey, PrivateEquityAssetKey

BTC_SATOSHIS = 100_000_000
ETH_GWEI = 1_000_000_000
DEFAULT_UNIT_QUANTA = 1_000_000
MONEY_FACTOR_SCALE = 1_000_000_000


def _exact_decimal(value: Any, *, field: str = "value") -> Decimal:
    """Parse an exact external decimal without silently accepting a float.

    Floats are deliberately rejected for scenario/API money inputs: converting
    a binary float through ``str`` merely hides the lossy boundary.  Model
    sampling has a separate, named quantization entrypoint below because it is
    the one intended float-to-money boundary.
    """

    if isinstance(value, float):
        raise TypeError(f"{field} must be an integer quantum count, Decimal, or decimal string; floats are not exact")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an exact decimal value") from exc
    if not decimal.is_finite():
        raise ValueError(f"{field} must be finite")
    return decimal


def validate_currency_amount(value: Any) -> Decimal:
    """Return one exact configured monetary amount.

    Configuration and wire inputs must spell money as an integer, ``Decimal``, or
    decimal string. A Python/JSON float is rejected rather than silently adopting
    its binary approximation. Model-produced floats use the separately named
    sampled-value quantization boundary below.
    """

    return _exact_decimal(value, field="currency amount")


def validate_currency_quantum(value: Any) -> Decimal:
    """Return a positive finite currency quantum from an exact value."""

    quantum = _exact_decimal(value, field="currency quantum")
    if quantum <= 0:
        raise ValueError(f"currency quantum must be positive; got {quantum}")
    return quantum


def currency_amount_to_quanta(value: Any, *, quantum: Any) -> np.int64:
    """Validate and convert a configured amount to integer currency quanta.

    Unlike sampled model output, configured money is never rounded: it must be
    exact and already representable by the scenario's declared quantum.
    """

    amount = validate_currency_amount(value)
    currency_quantum = validate_currency_quantum(quantum)
    count = amount / currency_quantum
    if count != count.to_integral_value():
        raise ValueError(f"{amount} is not an integer multiple of currency quantum {currency_quantum}")
    try:
        return np.int64(int(count))
    except OverflowError as exc:
        raise ValueError(f"currency quantum count {count} does not fit in int64") from exc


def round_currency_amount(value: Any, *, quantum: Any) -> Decimal:
    """Round an exact derived monetary calculation to the declared quantum.

    Configured amounts themselves are never rounded; use this only after exact
    arithmetic (percentages, allocation fractions, periodicization) produces a
    derived amount that must cross into the integer-money simulator.
    """

    amount = validate_currency_amount(value)
    currency_quantum = validate_currency_quantum(quantum)
    count = (amount / currency_quantum).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return count * currency_quantum


def ratio_to_money_factor(numerator: int | np.integer[Any], denominator: int | np.integer[Any]) -> np.int64:
    """Compile one exact integer ratio to the simulator's dimensionless factor scale."""

    numerator_int = int(numerator)
    denominator_int = int(denominator)
    if denominator_int <= 0:
        raise ValueError("money factor denominator must be positive")
    factor = (Decimal(numerator_int) * MONEY_FACTOR_SCALE / Decimal(denominator_int)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    try:
        return np.int64(int(factor))
    except OverflowError as exc:
        raise ValueError(f"money factor {factor} does not fit in int64") from exc


def sampled_array_to_quanta(values: Any, *, quantum: Any) -> Int64[np.ndarray, " ..."]:
    """Quantize a model-produced monetary path at the simulator boundary."""

    arr = np.asarray(values)
    out = np.empty(arr.shape, dtype=np.int64)
    currency_quantum = validate_currency_quantum(quantum)
    for idx in np.ndindex(arr.shape):
        try:
            sampled = Decimal(str(arr[idx]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("sampled monetary value must be numeric") from exc
        if not sampled.is_finite():
            raise ValueError("sampled monetary value must be finite")
        count = (sampled / currency_quantum).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        try:
            out[idx] = np.int64(int(count))
        except OverflowError as exc:
            raise ValueError(f"sampled currency quantum count {count} does not fit in int64") from exc
    return out


# Quantity quanta by symbol: the smallest fraction of a unit the ledger tracks. BTC and ETH
# are held in fractions far below a whole coin, so they get their native subdivision; everything
# else settles at the default. Per-symbol data, not per-asset-class: two crypto symbols already
# disagree here, and a fractional-share equity would join this table without needing a new type.
QUANTITY_SCALE_BY_SYMBOL: Mapping[str, int] = {"btc": BTC_SATOSHIS, "eth": ETH_GWEI}


def quantity_scale_for_asset(asset: AssetKey) -> int:
    if isinstance(asset, PrivateEquityAssetKey):
        return DEFAULT_UNIT_QUANTA
    return QUANTITY_SCALE_BY_SYMBOL.get(str(asset.symbol).lower(), DEFAULT_UNIT_QUANTA)


def quantity_to_quanta(value: Any, *, scale: int) -> np.int64:
    quanta = (Decimal(str(value)) * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return np.int64(quanta)


def quantity_array_to_quanta(values: Any, *, scale: int) -> Int64[np.ndarray, " ..."]:
    arr = np.asarray(values)
    out = np.empty(arr.shape, dtype=np.int64)
    for idx in np.ndindex(arr.shape):
        out[idx] = quantity_to_quanta(arr[idx], scale=scale)
    return out


def quanta_array_to_quantity(values: Any, *, scale: int) -> Float64[np.ndarray, " ..."]:
    return np.asarray(values, dtype=np.float64) / float(scale)
