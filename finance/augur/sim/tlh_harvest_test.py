from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.tlh_harvest import HarvestYieldParams, monthly_harvest_fraction, split_short_long

_PARAMS = HarvestYieldParams(
    peak_annual_yield=0.05,  # ~5%/yr first-year gross harvest, anchored to TY2025 1099-B
    floor_annual_yield=0.005,
    maturity_decay_exponent=1.5,
    drawdown_sensitivity=4.0,
)


def _arr(*values: float) -> np.ndarray:
    return np.array(values, dtype=np.float64)


def test_fresh_account_neutral_month_harvests_at_peak_rate() -> None:
    # e=0, flat month -> exactly the peak annual yield / 12, with no drawdown kicker.
    fraction = monthly_harvest_fraction(_arr(0.0), _arr(0.0), _PARAMS)
    np.testing.assert_allclose(fraction, _PARAMS.peak_annual_yield / 12.0)


def test_first_year_neutral_path_sums_to_peak_annual_anchor() -> None:
    # Twelve flat months on a fresh account should accumulate ~the first-year anchor (~5%).
    monthly = monthly_harvest_fraction(np.zeros(1), np.zeros(1), _PARAMS)[0]
    np.testing.assert_allclose(monthly * 12.0, _PARAMS.peak_annual_yield)


def test_yield_decays_monotonically_as_embedded_gain_rises() -> None:
    # Ossification: more embedded gain -> strictly less harvest (flat month, same return).
    e = _arr(0.0, 0.25, 0.5, 0.75, 1.0)
    fraction = monthly_harvest_fraction(np.zeros_like(e), e, _PARAMS)
    assert np.all(np.diff(fraction) < 0.0)


def test_fully_ossified_account_floors_out() -> None:
    fraction = monthly_harvest_fraction(_arr(0.0), _arr(1.0), _PARAMS)
    np.testing.assert_allclose(fraction, _PARAMS.floor_annual_yield / 12.0)


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


def test_split_short_long_conserves_total_and_respects_fraction() -> None:
    gross = _arr(1000.0, 2000.0)
    split = split_short_long(gross, _arr(1.0, 0.25))
    np.testing.assert_allclose(split.short_term_usd + split.long_term_usd, gross)
    # stf=1.0 -> all short-term (young account); 0.25 -> a quarter short-term.
    np.testing.assert_allclose(split.short_term_usd, _arr(1000.0, 500.0))


def test_floor_above_peak_is_rejected() -> None:
    with pytest.raises(ValueError, match="floor_annual_yield"):
        HarvestYieldParams(
            peak_annual_yield=0.01, floor_annual_yield=0.05, maturity_decay_exponent=1.0, drawdown_sensitivity=1.0
        )


if __name__ == "__main__":
    pytest_bazel.main()
