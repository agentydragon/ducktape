from __future__ import annotations

import numpy as np
import pytest_bazel

from finance.augur.model.series import CryptoKey, CryptoSymbol, SP500Key
from finance.augur.sim.fixed_point import (
    BTC_SATOSHIS,
    DEFAULT_UNIT_QUANTA,
    ETH_GWEI,
    cents_array_to_usd,
    quanta_array_to_quantity,
    quantity_array_to_quanta,
    quantity_scale_for_asset,
    quantity_to_quanta,
    usd_array_to_cents,
    usd_to_cents,
)


def test_usd_to_cents_uses_half_up_decimal_rounding() -> None:
    assert usd_to_cents("687.69") == np.int64(68_769)
    assert usd_to_cents("0.005") == np.int64(1)
    assert usd_to_cents("-0.005") == np.int64(-1)


def test_usd_array_round_trips_for_public_float_surface() -> None:
    cents = usd_array_to_cents(np.array([0.01, 1.23, 50_000.0]))
    np.testing.assert_array_equal(cents, np.array([1, 123, 5_000_000], dtype=np.int64))
    np.testing.assert_allclose(cents_array_to_usd(cents), np.array([0.01, 1.23, 50_000.0]))


def test_asset_quantity_scales_include_crypto_quanta() -> None:
    assert quantity_scale_for_asset(CryptoKey(symbol=CryptoSymbol("btc"))) == BTC_SATOSHIS
    assert quantity_scale_for_asset(CryptoKey(symbol=CryptoSymbol("eth"))) == ETH_GWEI
    assert quantity_scale_for_asset(SP500Key()) == DEFAULT_UNIT_QUANTA
    assert quantity_to_quanta("2.46761356", scale=BTC_SATOSHIS) == np.int64(246_761_356)
    assert quantity_to_quanta("43.31454407", scale=ETH_GWEI) == np.int64(43_314_544_070)


def test_quantity_array_converts_at_configured_scale() -> None:
    quanta = quantity_array_to_quanta(np.array([1.25, 2.0]), scale=DEFAULT_UNIT_QUANTA)
    np.testing.assert_array_equal(quanta, np.array([1_250_000, 2_000_000], dtype=np.int64))
    np.testing.assert_allclose(quanta_array_to_quantity(quanta, scale=DEFAULT_UNIT_QUANTA), np.array([1.25, 2.0]))


if __name__ == "__main__":
    pytest_bazel.main()
