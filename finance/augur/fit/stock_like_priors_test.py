from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.fit.stock_like_priors import (
    estimate_stock_like_return_prior,
    latest_world_bank_wide_csv_value,
    parse_fama_french_monthly_industry_returns_csv,
)


def test_parse_fama_french_monthly_industry_returns_csv() -> None:
    text = """This file was created by CMPT_IND_RETS using the 202604 CRSP database.
It contains value-weighted returns for industry portfolios.

,Food,Beer,Softw
192607, 0.56, -5.19, 2.31
192608, 2.72, 27.03, -99.99
192609, 1.58, 4.02, 1.20

Annual Returns:
"""

    returns = parse_fama_french_monthly_industry_returns_csv(text, portfolios=("Beer", "Softw"))

    assert returns.shape == (2, 2)
    np.testing.assert_allclose(returns[0], np.log1p(np.array([-5.19, 2.31]) / 100.0))
    np.testing.assert_allclose(returns[1], np.log1p(np.array([4.02, 1.20]) / 100.0))


def test_estimate_stock_like_return_prior() -> None:
    returns = np.array([[0.01, -0.02], [0.03, 0.04]], dtype=np.float64)

    estimate = estimate_stock_like_return_prior(returns)

    assert estimate.monthly_log_return_mu == pytest.approx(0.015)
    assert estimate.monthly_log_return_sigma > 0
    assert estimate.observation_count == 4


def test_latest_world_bank_wide_csv_value() -> None:
    text = """Country Name,Country Code,Indicator Name,Indicator Code,2022,2023,2024
World,WLD,GDP (current US$),NY.GDP.MKTP.CD,100000000000000,105000000000000,110000000000000
United States,USA,GDP (current US$),NY.GDP.MKTP.CD,25000000000000,27000000000000,
"""

    assert latest_world_bank_wide_csv_value(text, country_code="WLD", indicator_code="NY.GDP.MKTP.CD") == pytest.approx(
        110_000_000_000_000.0
    )


if __name__ == "__main__":
    pytest_bazel.main()
