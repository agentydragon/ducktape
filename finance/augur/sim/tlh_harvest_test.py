from __future__ import annotations

import math
import random

import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.tlh_harvest import PPB, HarvestYieldParams, _isqrt, _pow_half_ppb, monthly_harvest_fraction

_PARAMS = HarvestYieldParams(
    peak_annual_yield=0.05,  # ~5%/yr first-year gross harvest, anchored to TY2025 1099-B
    floor_annual_yield=0.005,
    maturity_decay_exponent=1.5,
    drawdown_sensitivity=4.0,
)


def _arr(*values: float) -> np.ndarray:
    return np.array(values, dtype=np.float64)


def _to_nearest_ppb(value: float) -> float:
    """The curve answers in whole parts per billion, so an expected rate lands there too."""

    return round(value * PPB) / PPB


def test_fresh_account_neutral_month_harvests_at_peak_rate() -> None:
    # e=0, flat month -> the peak annual yield / 12, with no drawdown kicker.
    fraction = monthly_harvest_fraction(_arr(0.0), _arr(0.0), _PARAMS)
    np.testing.assert_array_equal(fraction, _to_nearest_ppb(_PARAMS.peak_annual_yield / 12.0))


def test_first_year_neutral_path_sums_to_peak_annual_anchor() -> None:
    # Twelve flat months on a fresh account should accumulate ~the first-year anchor (~5%).
    monthly = monthly_harvest_fraction(np.zeros(1), np.zeros(1), _PARAMS)[0]
    np.testing.assert_allclose(monthly * 12.0, _PARAMS.peak_annual_yield, atol=12.0 / PPB)


def test_yield_decays_monotonically_as_embedded_gain_rises() -> None:
    # Ossification: more embedded gain -> strictly less harvest (flat month, same return).
    e = _arr(0.0, 0.25, 0.5, 0.75, 1.0)
    fraction = monthly_harvest_fraction(np.zeros_like(e), e, _PARAMS)
    assert np.all(np.diff(fraction) < 0.0)


def test_fully_ossified_account_floors_out() -> None:
    fraction = monthly_harvest_fraction(_arr(0.0), _arr(1.0), _PARAMS)
    np.testing.assert_array_equal(fraction, _to_nearest_ppb(_PARAMS.floor_annual_yield / 12.0))


def test_drawdowns_harvest_more_than_flat_or_up_months() -> None:
    e = np.zeros(3)
    # down 10%, flat, up 10% — same maturity, so ordering is purely the drawdown kicker.
    fraction = monthly_harvest_fraction(_arr(-0.10, 0.0, 0.10), e, _PARAMS)
    assert fraction[0] > fraction[1]
    # Up months get no kicker: positive returns clamp the drawdown term to zero.
    np.testing.assert_allclose(fraction[1], fraction[2])


def test_fraction_is_vectorized_over_rollouts() -> None:
    fraction = monthly_harvest_fraction(_arr(-0.2, 0.0), _arr(0.1, 0.9), _PARAMS)
    assert fraction.shape == (2,)
    assert np.all(fraction >= 0.0)


def test_floor_above_peak_is_rejected() -> None:
    with pytest.raises(ValueError, match="floor_annual_yield"):
        HarvestYieldParams(
            peak_annual_yield=0.01, floor_annual_yield=0.05, maturity_decay_exponent=1.0, drawdown_sensitivity=1.0
        )


def test_an_exponent_between_halves_is_rejected() -> None:
    """The integer curve raises to whole halves; anything else has no exact evaluation."""

    with pytest.raises(ValueError, match=r"multiple of 0\.5"):
        HarvestYieldParams(
            peak_annual_yield=0.05, floor_annual_yield=0.005, maturity_decay_exponent=1.3, drawdown_sensitivity=4.0
        )


def test_isqrt_matches_math_isqrt() -> None:
    """The claim the whole design rests on: the array square root is an exact floor.

    Rust reaches the same values through `i64::isqrt`, so the two engines agree only if
    this is exact rather than close — a seed one off at a perfect square would round the
    harvest fraction to a different part per billion and diverge the engines by a quantum.
    Perfect squares and their neighbours are where an inexact seed shows up, so they are
    checked explicitly alongside the sample.
    """

    edges = [0, 1, 2, 3, PPB - 1, PPB, PPB + 1, PPB * PPB - 1, PPB * PPB]
    squares = [root * root + offset for root in (1, 2, 3, 46_341, 999_999_999, PPB) for offset in (-1, 0, 1)]
    sample = random.Random(0).sample(range(PPB * PPB), 50_000)
    values = [value for value in [*edges, *squares, *sample] if 0 <= value <= PPB * PPB]

    computed = np.asarray(_isqrt(np.array(values, dtype=np.int64)))
    assert computed.tolist() == [math.isqrt(value) for value in values]


def test_a_half_integer_power_is_the_root_times_the_whole_powers() -> None:
    """`x ** 1.5` is `x * sqrt(x)`, which is why half-integers are the admitted set."""

    quarter = np.array([PPB // 4], dtype=np.int64)
    assert np.asarray(_pow_half_ppb(quarter, 2)).tolist() == [PPB // 4]  # x ** 1
    assert np.asarray(_pow_half_ppb(quarter, 1)).tolist() == [PPB // 2]  # sqrt(0.25) = 0.5
    assert np.asarray(_pow_half_ppb(quarter, 3)).tolist() == [PPB // 8]  # 0.25 ** 1.5 = 0.125


if __name__ == "__main__":
    pytest_bazel.main()
