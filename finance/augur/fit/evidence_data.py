from __future__ import annotations

import io
import json
import math
import os
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import polars as pl

from finance.augur.ingest import evidence_sources
from finance.augur.model.series import HomeValueKey, LocationId, RentKey

# CLEANUP(2026-06-09): the vendored-data fallback in _source_bytes (and the checked-in blobs) goes
#   once the deployment is proven reading from the git-synced evidence dir — see
#   augur/plans/evidence_git_ingestion.md.

# Home-value location -> (Zillow RegionName, State) for the ZHVI city rows to read.
ZILLOW_HOME_VALUE_REGIONS: dict[LocationId, tuple[str, str]] = {
    LocationId("san_francisco_ca"): ("San Francisco", "CA"),
    LocationId("vallejo_ca"): ("Vallejo", "CA"),
}

# Rent location -> (Zillow RegionName, State) for the ZORI city rows to read. Same Zillow
# methodology as home value, so the SF/Vallejo rent cross-covariance is sound. Mare Island
# carries no separate rent index; it mirrors vallejo_ca's path in the model.
ZILLOW_RENT_REGIONS: dict[LocationId, tuple[str, str]] = {
    LocationId("san_francisco_ca"): ("San Francisco", "CA"),
    LocationId("vallejo_ca"): ("Vallejo", "CA"),
}

# Minimum overlapping months required across the aligned exogenous factors.
MINIMUM_ALIGNED_MONTHS = 36


def _source_bytes(source: evidence_sources.EvidenceSource) -> bytes:
    """Raw bytes for an evidence series.

    The in-cluster deployment (`AUGUR_EVIDENCE_DIR` set) reads the file the git-sync sidecar
    keeps current under that directory, so the loader sees freshly-pulled evidence; a missing
    file raises (un-synced dir rather than stale data). Otherwise read the vendored copy as a
    `finance.augur` package resource, so it resolves whether augur is the Bazel main repo (its own
    tests) or an external module consumed downstream (e.g. gaffer via `archive_override`)."""
    if (evidence_dir := os.environ.get("AUGUR_EVIDENCE_DIR")) is not None:
        path = Path(evidence_dir) / source.output_filename
        if not path.exists():
            raise RuntimeError(f"augur evidence not found in AUGUR_EVIDENCE_DIR: {path}")
        return path.read_bytes()
    path = Path(str(resources.files("finance.augur").joinpath("data", source.output_filename)))
    if not path.exists():
        raise RuntimeError(f"augur vendored evidence not found: data/{source.output_filename}")
    return path.read_bytes()


MONTHS_PER_YEAR = 12
DATA_DERIVED_SERIES_PATH_PRIOR_SUFFIXES = ("_monthly_log_mu", "_monthly_log_mu_sigma", "_monthly_log_vol_sigma")
MIN_MONTHLY_LOG_MU_SIGMA = 0.005 / MONTHS_PER_YEAR
MIN_MONTHLY_LOG_VOL_SIGMA = 0.01 / math.sqrt(MONTHS_PER_YEAR)


@dataclass(frozen=True)
class PeriodReturns:
    log_returns: np.ndarray
    duration_months: np.ndarray


@dataclass(frozen=True)
class FactorSeriesCalibration:
    monthly_log_mu: float
    monthly_log_mu_sigma: float
    monthly_log_vol_sigma: float
    observed_months: float
    observation_count: int


@dataclass(frozen=True)
class ExogenousEvidence:
    factor_names: tuple[str, ...]
    monthly_log_returns: np.ndarray
    monthly_return_months: tuple[str, ...]
    marginal_returns: dict[str, PeriodReturns]
    series_path_calibration: dict[str, FactorSeriesCalibration]
    calibrated_series_path_priors: dict[str, float]
    current_mortgage30_rate_pct: float
    latest_observations: dict[str, Any]


def calibrate_series_path_priors(
    factor_names: tuple[str, ...], marginal_returns: dict[str, PeriodReturns]
) -> tuple[dict[str, FactorSeriesCalibration], dict[str, float]]:
    calibration: dict[str, FactorSeriesCalibration] = {}
    priors: dict[str, float] = {}
    for factor_name in factor_names:
        if factor_name not in marginal_returns:
            raise ValueError(f"missing marginal returns for exogenous factor {factor_name!r}")
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

        calibration[factor_name] = FactorSeriesCalibration(
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


def _read_fred_series(source: evidence_sources.EvidenceSource) -> pd.Series:
    column = source.series_id  # a FRED CSV is headed by its series id
    frame = pd.read_csv(io.BytesIO(_source_bytes(source)))
    if "observation_date" not in frame.columns or column not in frame.columns:
        raise ValueError(f"{source.provenance_label} must contain observation_date and {column}")
    dates = pd.to_datetime(frame["observation_date"], errors="raise")
    values = pd.to_numeric(frame[column], errors="coerce")
    series = pd.Series(values.to_numpy(dtype="float64"), index=dates).dropna()
    series = series[series > 0]
    if series.empty:
        raise ValueError(f"{source.provenance_label} contains no positive observations for {column}")
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


def _zillow_city_series(source: evidence_sources.EvidenceSource, *, region_name: str, state: str) -> pd.Series:
    # The Zillow city CSV is wide (a row per region, a column per month); polars reads + filters the
    # ~80 MB national file far faster than a Python row scan. Take the one city row, then unpivot its
    # date columns (named YYYY-MM-DD) into (month, value), keeping positive values.
    frame = pl.read_csv(_source_bytes(source), infer_schema=False)
    date_columns = [c for c in frame.columns if len(c) == 10 and c[4] == "-" and c[7] == "-"]
    row = frame.filter(
        (pl.col("RegionType") == "city") & (pl.col("RegionName") == region_name) & (pl.col("State") == state)
    )
    if row.is_empty():
        raise ValueError(f"{source.provenance_label} does not contain a city row for {region_name}, {state}")
    monthly = (
        row.head(1)
        .select(date_columns)
        .unpivot(variable_name="month", value_name="value")
        .with_columns(pl.col("value").cast(pl.Float64, strict=False))
        .drop_nulls("value")
        .filter(pl.col("value") > 0)
    )
    if monthly.is_empty():
        raise ValueError(f"{source.provenance_label} row for {region_name}, {state} contains no monthly values")
    months = [pd.Period(month, freq="M") for month in monthly["month"].to_list()]
    return pd.Series(monthly["value"].to_list(), index=months).sort_index()


def _read_yahoo_adjusted_close(source: evidence_sources.EvidenceSource, *, minimum_samples: int = 36) -> pd.Series:
    """Read a Yahoo-Finance v8 chart JSON down to `(timestamp -> adjusted_close)`.

    SPY's daily history has ~8k rows. Crypto histories may be monthly or weekly
    depending on what Yahoo serves under `range=max`; `minimum_samples` defaults
    to 36 so the loader accepts the coarser series as long as there's at least
    three years of data to fit on.
    """

    payload = json.loads(_source_bytes(source))
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        raise ValueError(f"{source.provenance_label} does not contain a Yahoo chart result")
    timestamps = result.get("timestamp") or []
    adjusted = (((result.get("indicators") or {}).get("adjclose") or [{}])[0]).get("adjclose") or []
    if len(timestamps) != len(adjusted):
        raise ValueError(f"{source.provenance_label} timestamp and adjusted-close arrays have different lengths")
    rows: dict[pd.Timestamp, float] = {}
    for timestamp, value in zip(timestamps, adjusted, strict=False):
        if value is None:
            continue
        number = float(value)
        if math.isfinite(number) and number > 0:
            dt = datetime.fromtimestamp(int(timestamp), tz=UTC).replace(tzinfo=None)
            rows[pd.Timestamp(dt.date())] = number
    if len(rows) < minimum_samples:
        raise ValueError(
            f"{source.provenance_label} did not yield a credible adjusted-close history ({len(rows)} samples < minimum {minimum_samples})"
        )
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


def load_exogenous_evidence() -> ExogenousEvidence:
    sp500_price = _monthly_last(_read_fred_series(evidence_sources.FRED_SP500))
    # SPY is daily; require thousands of rows to catch a truncated file.
    sp500_total_return = _monthly_last(_read_yahoo_adjusted_close(evidence_sources.YAHOO_SPY, minimum_samples=1000))
    btc_price = _monthly_last(_read_yahoo_adjusted_close(evidence_sources.YAHOO_BTC))
    eth_price = _monthly_last(_read_yahoo_adjusted_close(evidence_sources.YAHOO_ETH))
    cpi = _monthly_last(_read_fred_series(evidence_sources.FRED_CPI))
    case_shiller = _monthly_last(_read_fred_series(evidence_sources.FRED_SFXRSA))
    fhfa = _monthly_last(_read_fred_series(evidence_sources.FRED_FHFA_SF))
    mortgage30 = _read_fred_series(evidence_sources.FRED_MORTGAGE30)
    # Home-value and rent evidence stay keyed by LocationId (their magisterium-natural key); the
    # flat factor wire ids (HomeValueKey/RentKey .wire_id) are derived only at the matrix/JSON
    # boundaries below.
    home_values = {
        location_id: _zillow_city_series(evidence_sources.ZILLOW_ZHVI, region_name=region_name, state=state)
        for location_id, (region_name, state) in ZILLOW_HOME_VALUE_REGIONS.items()
    }
    rents = {
        location_id: _zillow_city_series(evidence_sources.ZILLOW_ZORI, region_name=region_name, state=state)
        for location_id, (region_name, state) in ZILLOW_RENT_REGIONS.items()
    }
    home_factor_wire_id = {location_id: HomeValueKey(location_id=location_id).wire_id for location_id in home_values}
    rent_factor_wire_id = {location_id: RentKey(location_id=location_id).wire_id for location_id in rents}
    home_factor_names = tuple(home_factor_wire_id.values())
    rent_factor_names = tuple(rent_factor_wire_id.values())
    factor_names = ("sp500", "crypto:btc", "crypto:eth", *home_factor_names, *rent_factor_names, "inflation")
    aligned = pd.concat(
        {
            "sp500": _monthly_unit_returns(sp500_total_return),
            "crypto:btc": _monthly_unit_returns(btc_price),
            "crypto:eth": _monthly_unit_returns(eth_price),
            **{home_factor_wire_id[loc]: _monthly_unit_returns(series) for loc, series in home_values.items()},
            **{rent_factor_wire_id[loc]: _monthly_unit_returns(series) for loc, series in rents.items()},
            "inflation": _monthly_unit_returns(cpi),
        },
        axis=1,
        join="inner",
    ).dropna()
    if len(aligned) < MINIMUM_ALIGNED_MONTHS:
        raise ValueError(f"only {len(aligned)} aligned exogenous months were available")

    sp500_returns = _period_return_frame(sp500_total_return)
    btc_returns = _period_return_frame(btc_price)
    eth_returns = _period_return_frame(eth_price)
    home_value_returns = {loc: _period_return_frame(series) for loc, series in home_values.items()}
    rent_returns = {loc: _period_return_frame(series) for loc, series in rents.items()}
    case_shiller_returns = _period_return_frame(case_shiller)
    fhfa_returns = _period_return_frame(fhfa)
    cpi_returns = _period_return_frame(cpi)
    marginal = {
        "sp500": _returns([sp500_returns]),
        "crypto:btc": _returns([btc_returns]),
        "crypto:eth": _returns([eth_returns]),
        **{home_factor_wire_id[loc]: _returns([returns]) for loc, returns in home_value_returns.items()},
        **{rent_factor_wire_id[loc]: _returns([returns]) for loc, returns in rent_returns.items()},
        "inflation": _returns([cpi_returns]),
    }
    series_path_calibration, calibrated_series_path_priors = calibrate_series_path_priors(factor_names, marginal)

    latest_observations = {
        "sp500_price_latest": {
            "date": str(sp500_price.index[-1]),
            "value": float(sp500_price.iloc[-1]),
            "source": evidence_sources.FRED_SP500.provenance_label,
        },
        "spy_adjusted_close_latest": {
            "date": str(sp500_total_return.index[-1]),
            "value": float(sp500_total_return.iloc[-1]),
            "source": evidence_sources.YAHOO_SPY.provenance_label,
        },
        "btc_close_latest": {
            "date": str(btc_price.index[-1]),
            "value": float(btc_price.iloc[-1]),
            "source": evidence_sources.YAHOO_BTC.provenance_label,
        },
        "eth_close_latest": {
            "date": str(eth_price.index[-1]),
            "value": float(eth_price.iloc[-1]),
            "source": evidence_sources.YAHOO_ETH.provenance_label,
        },
        "zillow_home_value_latest_by_factor": {
            home_factor_wire_id[loc]: {
                "date": str(series.index[-1]),
                "value": float(series.iloc[-1]),
                "source": evidence_sources.ZILLOW_ZHVI.provenance_label,
                "region_name": ZILLOW_HOME_VALUE_REGIONS[loc][0],
                "state": ZILLOW_HOME_VALUE_REGIONS[loc][1],
            }
            for loc, series in home_values.items()
        },
        "zillow_rent_latest_by_factor": {
            rent_factor_wire_id[loc]: {
                "date": str(series.index[-1]),
                "value": float(series.iloc[-1]),
                "source": evidence_sources.ZILLOW_ZORI.provenance_label,
                "region_name": ZILLOW_RENT_REGIONS[loc][0],
                "state": ZILLOW_RENT_REGIONS[loc][1],
            }
            for loc, series in rents.items()
        },
        "case_shiller_sf_latest": {
            "date": str(case_shiller.index[-1]),
            "value": float(case_shiller.iloc[-1]),
            "source": evidence_sources.FRED_SFXRSA.provenance_label,
        },
        "cpi_latest": {
            "date": str(cpi.index[-1]),
            "value": float(cpi.iloc[-1]),
            "source": evidence_sources.FRED_CPI.provenance_label,
        },
        "mortgage30_latest": {
            "date": mortgage30.index[-1].date().isoformat(),
            "value": float(mortgage30.iloc[-1]),
            "source": evidence_sources.FRED_MORTGAGE30.provenance_label,
        },
        "spy_adjusted_close_monthly_return_count": len(marginal["sp500"].log_returns),
        "housing_return_sources": {
            "zillow_city_zhvi_by_factor": {
                home_factor_wire_id[loc]: {
                    **_return_frame_summary(
                        returns, source=evidence_sources.ZILLOW_ZHVI.provenance_label, used_as_marginal_evidence=True
                    ),
                    "region_name": ZILLOW_HOME_VALUE_REGIONS[loc][0],
                    "state": ZILLOW_HOME_VALUE_REGIONS[loc][1],
                }
                for loc, returns in home_value_returns.items()
            },
            "case_shiller_sf_metro": _return_frame_summary(
                case_shiller_returns,
                source=evidence_sources.FRED_SFXRSA.provenance_label,
                used_as_marginal_evidence=False,
            ),
            "fhfa_sf_oakland_berkeley": _return_frame_summary(
                fhfa_returns, source=evidence_sources.FRED_FHFA_SF.provenance_label, used_as_marginal_evidence=False
            ),
        },
        "rent_return_sources": {
            "zillow_city_zori_by_factor": {
                rent_factor_wire_id[loc]: {
                    **_return_frame_summary(
                        returns, source=evidence_sources.ZILLOW_ZORI.provenance_label, used_as_marginal_evidence=True
                    ),
                    "region_name": ZILLOW_RENT_REGIONS[loc][0],
                    "state": ZILLOW_RENT_REGIONS[loc][1],
                }
                for loc, returns in rent_returns.items()
            }
        },
        "series_path_prior_calibration": {
            name: {
                "monthly_log_mu": point.monthly_log_mu,
                "monthly_log_mu_sigma": point.monthly_log_mu_sigma,
                "monthly_log_vol_sigma": point.monthly_log_vol_sigma,
                "observed_months": point.observed_months,
                "observation_count": point.observation_count,
            }
            for name, point in series_path_calibration.items()
        },
    }

    return ExogenousEvidence(
        factor_names=factor_names,
        monthly_log_returns=aligned.loc[:, list(factor_names)].to_numpy(dtype="float64"),
        monthly_return_months=tuple(str(period) for period in aligned.index),
        marginal_returns=marginal,
        series_path_calibration=series_path_calibration,
        calibrated_series_path_priors=calibrated_series_path_priors,
        current_mortgage30_rate_pct=float(mortgage30.iloc[-1]),
        latest_observations=latest_observations,
    )


@dataclass(frozen=True)
class MonthlyLevel:
    month: date  # first day of the calendar month the observation falls in
    value: float


def load_absolute_monthly_levels(wire_ids: Collection[str]) -> dict[str, list[MonthlyLevel]]:
    """Vendored absolute monthly level series (last observation per calendar month, oldest
    first) for each requested macro level wire id, on its real published scale.

    These read the same source files the exogenous evidence fits against, but at their
    absolute level rather than as log-returns, so calibration can anchor a sampled path's
    month 0 to the real spot. Raises `KeyError` for a wire id with no vendored absolute
    source (e.g. home-value series, which are not anchored against today)."""
    out: dict[str, list[MonthlyLevel]] = {}
    for wire in wire_ids:
        match wire:
            case "sp500":
                raw = _read_fred_series(evidence_sources.FRED_SP500)
            case "inflation":
                raw = _read_fred_series(evidence_sources.FRED_CPI)
            case "crypto:btc":
                raw = _read_yahoo_adjusted_close(evidence_sources.YAHOO_BTC)
            case "crypto:eth":
                raw = _read_yahoo_adjusted_close(evidence_sources.YAHOO_ETH)
            case "rent:san_francisco_ca":
                raw = _read_fred_series(evidence_sources.FRED_SF_RENT_CPI)
            case _:
                raise KeyError(f"no vendored absolute level series for level wire id {wire!r}")
        monthly = _monthly_last(raw)
        # `monthly` is period-indexed (grouped to "M"); pandas-stubs widens the index to Hashable,
        # so name the Period explicitly to reach `.to_timestamp()`.
        out[wire] = [
            MonthlyLevel(month=cast(pd.Period, period).to_timestamp().date(), value=float(value))
            for period, value in monthly.items()
        ]
    return out
