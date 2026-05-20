from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from python.runfiles import runfiles

from augur.fit.market_config import SourceDataConfig

_RUNFILES_PREFIX = "runfiles:"


def resolve_path(path: str | Path, base_dir: str | Path) -> Path:
    """Resolve `path` against `base_dir` if relative, return as-is if absolute.

    The `runfiles:<rlocation>` prefix routes through the Bazel runfiles library
    instead — used so config files can reference ducktape-side data CSVs from a
    deployment-repo config without duplicating the CSVs alongside the config."""
    path_str = str(path)
    if path_str.startswith(_RUNFILES_PREFIX):
        rlocation = path_str[len(_RUNFILES_PREFIX) :]
        r = runfiles.Create()
        resolved = r.Rlocation(rlocation) if r is not None else None
        if not resolved:
            raise RuntimeError(f"Could not resolve runfiles path: {rlocation}")
        return Path(resolved)
    p = Path(path)
    if p.is_absolute():
        return p
    return (Path(base_dir) / p).resolve()


BASE_HOME_VALUE_FACTOR = "home"
MONTHS_PER_YEAR = 12
DATA_DERIVED_MARKET_PATH_PRIOR_SUFFIXES = ("_monthly_log_mu", "_monthly_log_mu_sigma", "_monthly_log_vol_sigma")
MIN_MONTHLY_LOG_MU_SIGMA = 0.005 / MONTHS_PER_YEAR
MIN_MONTHLY_LOG_VOL_SIGMA = 0.01 / math.sqrt(MONTHS_PER_YEAR)


@dataclass(frozen=True)
class PeriodReturns:
    log_returns: np.ndarray
    duration_months: np.ndarray


@dataclass(frozen=True)
class FactorMarketCalibration:
    monthly_log_mu: float
    monthly_log_mu_sigma: float
    monthly_log_vol_sigma: float
    observed_months: float
    observation_count: int


@dataclass(frozen=True)
class MarketEvidence:
    factor_names: tuple[str, ...]
    monthly_log_returns: np.ndarray
    monthly_return_months: tuple[str, ...]
    marginal_returns: dict[str, PeriodReturns]
    market_path_calibration: dict[str, FactorMarketCalibration]
    calibrated_market_path_priors: dict[str, float]
    current_mortgage30_rate_pct: float
    latest_observations: dict[str, Any]


def calibrate_market_path_priors(
    factor_names: tuple[str, ...], marginal_returns: dict[str, PeriodReturns]
) -> tuple[dict[str, FactorMarketCalibration], dict[str, float]]:
    calibration: dict[str, FactorMarketCalibration] = {}
    priors: dict[str, float] = {}
    for factor_name in factor_names:
        if factor_name not in marginal_returns:
            raise ValueError(f"missing marginal returns for market factor {factor_name!r}")
        returns = marginal_returns[factor_name]
        log_returns = np.asarray(returns.log_returns, dtype="float64")
        duration_months = np.asarray(returns.duration_months, dtype="float64")
        if log_returns.shape != duration_months.shape:
            raise ValueError(f"{factor_name} marginal returns and durations have different shapes")
        keep = np.isfinite(log_returns) & np.isfinite(duration_months) & (duration_months > 0)
        log_returns = log_returns[keep]
        duration_months = duration_months[keep]
        if len(log_returns) == 0:
            raise ValueError(f"{factor_name} has no finite marginal returns")

        observed_months = float(np.sum(duration_months))
        monthly_log_mu = float(np.sum(log_returns)) / observed_months
        normalized_residuals = (log_returns - monthly_log_mu * duration_months) / np.sqrt(duration_months)
        if len(normalized_residuals) > 1:
            monthly_vol = float(np.std(normalized_residuals, ddof=1))
        else:
            monthly_vol = float(abs(normalized_residuals[0]))
        monthly_log_vol_sigma = max(monthly_vol, MIN_MONTHLY_LOG_VOL_SIGMA)
        monthly_log_mu_sigma = max(
            monthly_log_vol_sigma / math.sqrt(max(observed_months, 1e-9)), MIN_MONTHLY_LOG_MU_SIGMA
        )

        calibration[factor_name] = FactorMarketCalibration(
            monthly_log_mu=monthly_log_mu,
            monthly_log_mu_sigma=monthly_log_mu_sigma,
            monthly_log_vol_sigma=monthly_log_vol_sigma,
            observed_months=observed_months,
            observation_count=len(log_returns),
        )
        priors[f"{factor_name}_monthly_log_mu"] = monthly_log_mu
        priors[f"{factor_name}_monthly_log_mu_sigma"] = monthly_log_mu_sigma
        priors[f"{factor_name}_monthly_log_vol_sigma"] = monthly_log_vol_sigma
    return calibration, priors


def _read_fred_series(path: Path, column: str) -> pd.Series:
    frame = pd.read_csv(path)
    if "observation_date" not in frame.columns or column not in frame.columns:
        raise ValueError(f"{path} must contain observation_date and {column}")
    dates = pd.to_datetime(frame["observation_date"], errors="raise")
    values = pd.to_numeric(frame[column], errors="coerce")
    series = pd.Series(values.to_numpy(dtype="float64"), index=dates).dropna()
    series = series[series > 0]
    if series.empty:
        raise ValueError(f"{path} contains no positive observations for {column}")
    return series.sort_index()


def _monthly_last(series: pd.Series) -> pd.Series:
    assert isinstance(series.index, pd.DatetimeIndex), f"expected DatetimeIndex, got {type(series.index).__name__}"
    out = series.groupby(series.index.to_period("M")).last().dropna()
    out = out[out > 0]
    if out.empty:
        raise ValueError("monthly series contains no positive observations")
    return out


def _period_number(period: pd.Period) -> int:
    return int(period.year) * 12 + int(period.month)


def _period_return_frame(series: pd.Series) -> pd.DataFrame:
    series = series.dropna()
    series = series[series > 0]
    if len(series) < 2:
        raise ValueError("series needs at least two positive observations")
    periods = np.array([_period_number(period) for period in series.index], dtype="int64")
    values = np.log(series.to_numpy(dtype="float64"))
    durations = np.diff(periods)
    returns = np.diff(values)
    keep = np.isfinite(returns) & (durations > 0)
    return pd.DataFrame(
        {"log_return": returns[keep], "duration_months": durations[keep].astype("float64")},
        index=series.index[1:][keep],
    )


def _monthly_unit_returns(series: pd.Series) -> pd.Series:
    frame = _period_return_frame(series)
    return frame.loc[frame["duration_months"] == 1, "log_return"]


def _zillow_city_series(path: Path, *, region_name: str, state: str) -> pd.Series:
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("RegionType") == "city" and row.get("RegionName") == region_name and row.get("State") == state:
                values: dict[pd.Period, float] = {}
                for key, raw in row.items():
                    if not key or len(key) != 10 or key[4] != "-" or key[7] != "-":
                        continue
                    if raw is None or not raw.strip():
                        continue
                    try:
                        value = float(raw)
                    except ValueError:
                        continue
                    if value > 0:
                        values[pd.Period(pd.Timestamp(key), freq="M")] = value
                if not values:
                    raise ValueError(f"{path} row for {region_name}, {state} contains no monthly values")
                return pd.Series(values).sort_index()
    raise ValueError(f"{path} does not contain a city row for {region_name}, {state}")


def _home_value_region_config(source_config: SourceDataConfig) -> dict[str, tuple[str, str]]:
    regions = {
        factor_name: (region.region_name, region.state)
        for factor_name, region in source_config.zillow_home_value_regions.items()
    }
    if BASE_HOME_VALUE_FACTOR not in regions:
        raise ValueError(f"source_data.zillow_home_value_regions must include {BASE_HOME_VALUE_FACTOR!r}")
    return regions


def _read_yahoo_spy_adjusted_close(path: Path) -> pd.Series:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        raise ValueError(f"{path} does not contain a Yahoo chart result")
    timestamps = result.get("timestamp") or []
    adjusted = (((result.get("indicators") or {}).get("adjclose") or [{}])[0]).get("adjclose") or []
    if len(timestamps) != len(adjusted):
        raise ValueError(f"{path} timestamp and adjusted-close arrays have different lengths")
    rows: dict[pd.Timestamp, float] = {}
    for timestamp, value in zip(timestamps, adjusted, strict=False):
        if value is None:
            continue
        number = float(value)
        if math.isfinite(number) and number > 0:
            dt = datetime.fromtimestamp(int(timestamp), tz=UTC).replace(tzinfo=None)
            rows[pd.Timestamp(dt.date())] = number
    if len(rows) < 1000:
        raise ValueError(f"{path} did not yield a credible SPY adjusted-close history")
    return pd.Series(rows).sort_index()


def _returns(values: list[pd.DataFrame]) -> PeriodReturns:
    frame = pd.concat(values, ignore_index=True)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if frame.empty:
        raise ValueError("no observed returns were loaded")
    return PeriodReturns(
        log_returns=frame["log_return"].to_numpy(dtype="float64"),
        duration_months=frame["duration_months"].to_numpy(dtype="float64"),
    )


def _return_frame_summary(frame: pd.DataFrame, *, source: str, used_as_marginal_evidence: bool) -> dict[str, object]:
    return {
        "source": source,
        "used_as_marginal_evidence": used_as_marginal_evidence,
        "return_count": len(frame),
        "first_return_month": str(frame.index.min()),
        "last_return_month": str(frame.index.max()),
        "min_duration_months": float(frame["duration_months"].min()),
        "max_duration_months": float(frame["duration_months"].max()),
    }


def load_market_evidence(source_config: SourceDataConfig, base_dir: str | Path) -> MarketEvidence:
    sp500_price = _monthly_last(_read_fred_series(resolve_path(source_config.fred_sp500_csv, base_dir), "SP500"))
    sp500_total_return = _monthly_last(
        _read_yahoo_spy_adjusted_close(resolve_path(source_config.yahoo_spy_adjusted_json, base_dir))
    )
    cpi = _monthly_last(_read_fred_series(resolve_path(source_config.fred_cpi_us_csv, base_dir), "CPIAUCSL"))
    rent = _monthly_last(_read_fred_series(resolve_path(source_config.fred_sf_rent_cpi_csv, base_dir), "CUURA422SEHA"))
    case_shiller = _monthly_last(_read_fred_series(resolve_path(source_config.fred_sfxrsa_csv, base_dir), "SFXRSA"))
    fhfa = _monthly_last(
        _read_fred_series(resolve_path(source_config.fred_fhfa_sf_oakland_berkeley_csv, base_dir), "ATNHPIUS41884Q")
    )
    mortgage30 = _read_fred_series(resolve_path(source_config.fred_mortgage30_csv, base_dir), "MORTGAGE30US")
    zillow_path = resolve_path(source_config.zillow_city_zhvi_csv, base_dir)
    home_value_region_config = _home_value_region_config(source_config)
    home_values = {
        factor_name: _zillow_city_series(zillow_path, region_name=region_name, state=state)
        for factor_name, (region_name, state) in home_value_region_config.items()
    }
    home_factor_names = tuple(home_values)
    market_factors = ("sp500", *home_factor_names, "rent", "inflation")
    aligned = pd.concat(
        {
            "sp500": _monthly_unit_returns(sp500_total_return),
            **{factor_name: _monthly_unit_returns(series) for factor_name, series in home_values.items()},
            "rent": _monthly_unit_returns(rent),
            "inflation": _monthly_unit_returns(cpi),
        },
        axis=1,
        join="inner",
    ).dropna()
    if len(aligned) < source_config.minimum_aligned_months:
        raise ValueError(f"only {len(aligned)} aligned market months were available")

    sp500_returns = _period_return_frame(sp500_total_return)
    home_value_returns = {factor_name: _period_return_frame(series) for factor_name, series in home_values.items()}
    case_shiller_returns = _period_return_frame(case_shiller)
    fhfa_returns = _period_return_frame(fhfa)
    rent_returns = _period_return_frame(rent)
    cpi_returns = _period_return_frame(cpi)
    marginal = {
        "sp500": _returns([sp500_returns]),
        **{factor_name: _returns([returns]) for factor_name, returns in home_value_returns.items()},
        "rent": _returns([rent_returns]),
        "inflation": _returns([cpi_returns]),
    }
    market_path_calibration, calibrated_market_path_priors = calibrate_market_path_priors(market_factors, marginal)

    latest_observations = {
        "sp500_price_latest": {
            "date": str(sp500_price.index[-1]),
            "value": float(sp500_price.iloc[-1]),
            "source": source_config.fred_sp500_csv,
        },
        "spy_adjusted_close_latest": {
            "date": str(sp500_total_return.index[-1]),
            "value": float(sp500_total_return.iloc[-1]),
            "source": source_config.yahoo_spy_adjusted_json,
        },
        "zillow_home_value_latest_by_factor": {
            factor_name: {
                "date": str(series.index[-1]),
                "value": float(series.iloc[-1]),
                "source": source_config.zillow_city_zhvi_csv,
                "region_name": home_value_region_config[factor_name][0],
                "state": home_value_region_config[factor_name][1],
            }
            for factor_name, series in home_values.items()
        },
        "case_shiller_sf_latest": {
            "date": str(case_shiller.index[-1]),
            "value": float(case_shiller.iloc[-1]),
            "source": source_config.fred_sfxrsa_csv,
        },
        "sf_rent_cpi_latest": {
            "date": str(rent.index[-1]),
            "value": float(rent.iloc[-1]),
            "source": source_config.fred_sf_rent_cpi_csv,
        },
        "cpi_latest": {
            "date": str(cpi.index[-1]),
            "value": float(cpi.iloc[-1]),
            "source": source_config.fred_cpi_us_csv,
        },
        "mortgage30_latest": {
            "date": mortgage30.index[-1].date().isoformat(),
            "value": float(mortgage30.iloc[-1]),
            "source": source_config.fred_mortgage30_csv,
        },
        "spy_adjusted_close_monthly_return_count": len(marginal["sp500"].log_returns),
        "housing_return_sources": {
            "zillow_city_zhvi_by_factor": {
                factor_name: {
                    **_return_frame_summary(
                        returns, source=source_config.zillow_city_zhvi_csv, used_as_marginal_evidence=True
                    ),
                    "region_name": home_value_region_config[factor_name][0],
                    "state": home_value_region_config[factor_name][1],
                }
                for factor_name, returns in home_value_returns.items()
            },
            "case_shiller_sf_metro": _return_frame_summary(
                case_shiller_returns, source=source_config.fred_sfxrsa_csv, used_as_marginal_evidence=False
            ),
            "fhfa_sf_oakland_berkeley": _return_frame_summary(
                fhfa_returns, source=source_config.fred_fhfa_sf_oakland_berkeley_csv, used_as_marginal_evidence=False
            ),
        },
        "market_path_prior_calibration": {
            name: {
                "monthly_log_mu": point.monthly_log_mu,
                "monthly_log_mu_sigma": point.monthly_log_mu_sigma,
                "monthly_log_vol_sigma": point.monthly_log_vol_sigma,
                "observed_months": point.observed_months,
                "observation_count": point.observation_count,
            }
            for name, point in market_path_calibration.items()
        },
    }

    return MarketEvidence(
        factor_names=market_factors,
        monthly_log_returns=aligned.loc[:, list(market_factors)].to_numpy(dtype="float64"),
        monthly_return_months=tuple(str(period) for period in aligned.index),
        marginal_returns=marginal,
        market_path_calibration=market_path_calibration,
        calibrated_market_path_priors=calibrated_market_path_priors,
        current_mortgage30_rate_pct=float(mortgage30.iloc[-1]),
        latest_observations=latest_observations,
    )
