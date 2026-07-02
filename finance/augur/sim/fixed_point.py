"""Fixed-point helpers for Augur's engine-internal accounting.

Public scenario/config/product surfaces still speak in dollars and units as
floats. The dense engine uses integer quanta so cash/accounting paths do not
depend on binary floating-point exactness.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
from numpy.typing import NDArray

from finance.augur.product.asset_key import AssetKey, CryptoAssetKey

USD_CENTS = 100
BTC_SATOSHIS = 100_000_000
ETH_GWEI = 1_000_000_000
DEFAULT_UNIT_QUANTA = 1_000_000


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def usd_to_cents(value: Any) -> np.int64:
    cents = (_decimal(value) * USD_CENTS).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return np.int64(cents)


def cents_to_usd(value: Any) -> float:
    return float(np.asarray(value, dtype=np.float64) / float(USD_CENTS))


def usd_array_to_cents(values: Any) -> NDArray[np.int64]:
    arr = np.asarray(values)
    out = np.empty(arr.shape, dtype=np.int64)
    for idx in np.ndindex(arr.shape):
        out[idx] = usd_to_cents(arr[idx])
    return out


def cents_array_to_usd(values: Any) -> NDArray[np.float64]:
    return np.asarray(values, dtype=np.float64) / float(USD_CENTS)


def quantity_scale_for_asset(asset: AssetKey) -> int:
    if isinstance(asset, CryptoAssetKey):
        symbol = str(asset.symbol).lower()
        if symbol == "btc":
            return BTC_SATOSHIS
        if symbol == "eth":
            return ETH_GWEI
    return DEFAULT_UNIT_QUANTA


def quantity_to_quanta(value: Any, *, scale: int) -> np.int64:
    quanta = (_decimal(value) * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return np.int64(quanta)


def quantity_array_to_quanta(values: Any, *, scale: int) -> NDArray[np.int64]:
    arr = np.asarray(values)
    out = np.empty(arr.shape, dtype=np.int64)
    for idx in np.ndindex(arr.shape):
        out[idx] = quantity_to_quanta(arr[idx], scale=scale)
    return out


def quanta_array_to_quantity(values: Any, *, scale: int) -> NDArray[np.float64]:
    return np.asarray(values, dtype=np.float64) / float(scale)
