"""Helpers for source-backed stock-like private-equity priors."""

from __future__ import annotations

import csv
import math
from io import StringIO

import numpy as np
from pydantic import Field

from finance.augur.model.schemas import FrozenModel


class StockLikeReturnPriorEstimate(FrozenModel):
    monthly_log_return_mu: float
    monthly_log_return_sigma: float = Field(gt=0)
    observation_count: int = Field(ge=1)


def parse_fama_french_monthly_industry_returns_csv(text: str, *, portfolios: tuple[str, ...]) -> np.ndarray:
    """Parse monthly percent returns from a Kenneth French industry CSV file.

    Source files:
    - Fama/French Data Library:
      https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html
    - Example direct monthly industry ZIP:
      https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/30_Industry_Portfolios_CSV.zip

    The upstream CSV has a prose preamble followed by a monthly table and then
    annual tables. Missing values are encoded as -99.99 or -999. This parser
    returns monthly log returns in decimal units for the selected portfolios.
    """

    if not portfolios:
        raise ValueError("at least one Fama/French portfolio name is required")
    reader = csv.reader(StringIO(text))
    header: list[str] | None = None
    indexes: list[int] = []
    rows: list[list[float]] = []
    for raw_row in reader:
        row = [cell.strip() for cell in raw_row]
        if not row:
            continue
        if header is None:
            if row[0] == "" and all(portfolio in row for portfolio in portfolios):
                header = row
                indexes = [row.index(portfolio) for portfolio in portfolios]
            continue
        period = row[0]
        if len(period) != 6 or not period.isdigit():
            break
        values: list[float] = []
        missing = False
        for index in indexes:
            try:
                percent_return = float(row[index])
            except (IndexError, ValueError) as error:
                raise ValueError(f"invalid Fama/French return row for {period}: {row}") from error
            if percent_return in {-99.99, -999.0}:
                missing = True
                break
            values.append(math.log1p(percent_return / 100.0))
        if not missing:
            rows.append(values)
    if header is None:
        raise ValueError(f"Fama/French CSV did not contain requested portfolios {portfolios}")
    if not rows:
        raise ValueError("Fama/French CSV did not contain usable monthly returns")
    return np.asarray(rows, dtype=np.float64)


def estimate_stock_like_return_prior(monthly_log_returns: np.ndarray) -> StockLikeReturnPriorEstimate:
    values = np.asarray(monthly_log_returns, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2 or values.size == 0:
        raise ValueError("monthly_log_returns must be a non-empty 1D or 2D array")
    finite = values[np.isfinite(values)]
    if finite.size < 2:
        raise ValueError("need at least two finite monthly returns to estimate a stock-like prior")
    return StockLikeReturnPriorEstimate(
        monthly_log_return_mu=float(np.mean(finite)),
        monthly_log_return_sigma=float(np.std(finite, ddof=1)),
        observation_count=int(finite.size),
    )


def latest_world_bank_wide_csv_value(text: str, *, country_code: str, indicator_code: str) -> float:
    """Read the latest positive value from a World Bank wide-format CSV export.

    Source:
    - World Bank GDP current USD, indicator NY.GDP.MKTP.CD:
      https://data.worldbank.org/indicator/NY.GDP.MKTP.CD

    The downloadable World Bank CSV is a wide table with metadata columns and
    one column per year. This returns the latest positive numeric value for the
    requested country/indicator pair.
    """

    reader = csv.DictReader(StringIO(text))
    for row in reader:
        if row.get("Country Code") != country_code or row.get("Indicator Code") != indicator_code:
            continue
        year_values: list[tuple[int, float]] = []
        for key, raw_value in row.items():
            if key is None or len(key) != 4 or not key.isdigit() or raw_value is None or not raw_value.strip():
                continue
            value = float(raw_value)
            if math.isfinite(value) and value > 0:
                year_values.append((int(key), value))
        if not year_values:
            raise ValueError(f"World Bank row {country_code}/{indicator_code} has no positive annual values")
        return max(year_values)[1]
    raise ValueError(f"World Bank CSV missing row {country_code}/{indicator_code}")
