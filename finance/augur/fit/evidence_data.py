from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass
from functools import reduce
from typing import Any

import numpy as np
import polars as pl

from finance.augur.model.series import SP500_KEY, HomeValueKey, LocationId, RentKey, SecurityKey, SecuritySymbol
from finance.evidence import loading, sources
from finance.evidence.loading import MonthlyLevel

# The crypto symbols this fit models. Which securities get fitted is the fit's own choice;
# the series type does not distinguish them from any other symbol.
BTC_KEY = SecurityKey(symbol=SecuritySymbol("btc"))
ETH_KEY = SecurityKey(symbol=SecuritySymbol("eth"))

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


def _source_bytes(source: sources.EvidenceSource) -> bytes:
    """Raw bytes for an evidence series, read from the git-synced evidence directory.

    The git-sync sidecar keeps `AUGUR_EVIDENCE_DIR` pointed at the augur-evidence repo's current
    worktree, so each read reflects freshly-pulled data. Kept as a module seam so tests can inject
    malformed bytes per source (see `finance/augur/fit/data_test.py`)."""
    return loading.source_bytes(loading.evidence_dir_from_env(), source)


def _fred_frame(source: sources.EvidenceSource) -> pl.DataFrame:
    return loading.fred_series_frame(_source_bytes(source), source)


def _yahoo_frame(source: sources.EvidenceSource, *, minimum_samples: int = 36) -> pl.DataFrame:
    return loading.yahoo_adjusted_close_frame(_source_bytes(source), source, minimum_samples=minimum_samples)


def _zillow_frame(source: sources.EvidenceSource, *, region_name: str, state: str) -> pl.DataFrame:
    return loading.zillow_city_series_frame(_source_bytes(source), source, region_name=region_name, state=state)


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


# Evidence series are carried as 2-column polars frames: raw series as `(date, value)`, and the
# monthly resamplings/returns below as `(month, ...)` where `month` is the first day of the month.


def _period_return_frame(series: pl.DataFrame) -> pl.DataFrame:
    """Log-returns of a `(month, value)` frame, with month-gap durations, keyed by the later month."""
    series = series.filter(pl.col("value") > 0).sort("month")
    if series.height < 2:
        raise ValueError("series needs at least two positive observations")
    month_number = pl.col("month").dt.year() * 12 + pl.col("month").dt.month()
    return (
        series.select(
            pl.col("month"),
            pl.col("value").log().diff().alias("log_return"),
            month_number.diff().cast(pl.Float64).alias("duration_months"),
        )
        .drop_nulls()  # the first row has a null diff
        .filter(pl.col("log_return").is_finite() & (pl.col("duration_months") > 0))
    )


def _monthly_unit_returns(series: pl.DataFrame) -> pl.DataFrame:
    """`(month, log_return)` for the consecutive-month (duration == 1) steps of a monthly series."""
    return _period_return_frame(series).filter(pl.col("duration_months") == 1).select("month", "log_return")


def _returns(frame: pl.DataFrame) -> PeriodReturns:
    frame = frame.filter(pl.col("log_return").is_finite() & pl.col("duration_months").is_finite())
    if frame.is_empty():
        raise ValueError("no observed returns were loaded")
    return PeriodReturns(
        log_returns=frame["log_return"].to_numpy().astype("float64"),
        duration_months=frame["duration_months"].to_numpy().astype("float64"),
    )


def _return_frame_summary(frame: pl.DataFrame, *, source: str, used_as_marginal_evidence: bool) -> dict[str, object]:
    months = frame["month"].to_list()
    durations = frame["duration_months"].to_list()
    return {
        "source": source,
        "used_as_marginal_evidence": used_as_marginal_evidence,
        "return_count": frame.height,
        "first_return_month": min(months).strftime("%Y-%m"),
        "last_return_month": max(months).strftime("%Y-%m"),
        "min_duration_months": float(min(durations)),
        "max_duration_months": float(max(durations)),
    }


def _align_inner(frames: dict[str, pl.DataFrame], value_column: str) -> pl.DataFrame:
    """Inner-join `{name: (month, <value_column>)}` frames on month, one renamed column per name."""
    renamed = [frame.rename({value_column: name}) for name, frame in frames.items()]
    return reduce(lambda left, right: left.join(right, on="month", how="inner"), renamed).drop_nulls().sort("month")


def _monthly_latest(monthly: pl.DataFrame, source: sources.EvidenceSource) -> dict[str, object]:
    return {
        "date": monthly["month"].to_list()[-1].strftime("%Y-%m"),
        "value": float(monthly["value"].to_list()[-1]),
        "source": source.provenance_label,
    }


def load_exogenous_evidence() -> ExogenousEvidence:
    sp500_price = loading.monthly_last(_fred_frame(sources.FRED_SP500))
    # SPY is daily; require thousands of rows to catch a truncated file.
    sp500_total_return = loading.monthly_last(_yahoo_frame(sources.YAHOO_SPY, minimum_samples=1000))
    btc_price = loading.monthly_last(_yahoo_frame(sources.YAHOO_BTC))
    eth_price = loading.monthly_last(_yahoo_frame(sources.YAHOO_ETH))
    cpi = loading.monthly_last(_fred_frame(sources.FRED_CPI))
    case_shiller = loading.monthly_last(_fred_frame(sources.FRED_SFXRSA))
    fhfa = loading.monthly_last(_fred_frame(sources.FRED_FHFA_SF))
    mortgage30 = _fred_frame(sources.FRED_MORTGAGE30)
    # Home-value and rent evidence stay keyed by LocationId (their role-natural key); the
    # flat factor wire ids (HomeValueKey/RentKey .wire_id) are derived only at the matrix/JSON
    # boundaries below.
    home_values = {
        location_id: _zillow_frame(sources.ZILLOW_ZHVI, region_name=region_name, state=state)
        for location_id, (region_name, state) in ZILLOW_HOME_VALUE_REGIONS.items()
    }
    rents = {
        location_id: _zillow_frame(sources.ZILLOW_ZORI, region_name=region_name, state=state)
        for location_id, (region_name, state) in ZILLOW_RENT_REGIONS.items()
    }
    home_factor_wire_id = {location_id: HomeValueKey(location_id=location_id).wire_id for location_id in home_values}
    rent_factor_wire_id = {location_id: RentKey(location_id=location_id).wire_id for location_id in rents}
    home_factor_names = tuple(home_factor_wire_id.values())
    rent_factor_names = tuple(rent_factor_wire_id.values())
    factor_names = (
        SP500_KEY.wire_id,
        BTC_KEY.wire_id,
        ETH_KEY.wire_id,
        *home_factor_names,
        *rent_factor_names,
        "inflation",
    )
    aligned = _align_inner(
        {
            SP500_KEY.wire_id: _monthly_unit_returns(sp500_total_return),
            BTC_KEY.wire_id: _monthly_unit_returns(btc_price),
            ETH_KEY.wire_id: _monthly_unit_returns(eth_price),
            **{home_factor_wire_id[loc]: _monthly_unit_returns(series) for loc, series in home_values.items()},
            **{rent_factor_wire_id[loc]: _monthly_unit_returns(series) for loc, series in rents.items()},
            "inflation": _monthly_unit_returns(cpi),
        },
        value_column="log_return",
    )
    if aligned.height < MINIMUM_ALIGNED_MONTHS:
        raise ValueError(f"only {aligned.height} aligned exogenous months were available")

    sp500_returns = _period_return_frame(sp500_total_return)
    btc_returns = _period_return_frame(btc_price)
    eth_returns = _period_return_frame(eth_price)
    home_value_returns = {loc: _period_return_frame(series) for loc, series in home_values.items()}
    rent_returns = {loc: _period_return_frame(series) for loc, series in rents.items()}
    case_shiller_returns = _period_return_frame(case_shiller)
    fhfa_returns = _period_return_frame(fhfa)
    cpi_returns = _period_return_frame(cpi)
    marginal = {
        SP500_KEY.wire_id: _returns(sp500_returns),
        BTC_KEY.wire_id: _returns(btc_returns),
        ETH_KEY.wire_id: _returns(eth_returns),
        **{home_factor_wire_id[loc]: _returns(returns) for loc, returns in home_value_returns.items()},
        **{rent_factor_wire_id[loc]: _returns(returns) for loc, returns in rent_returns.items()},
        "inflation": _returns(cpi_returns),
    }
    series_path_calibration, calibrated_series_path_priors = calibrate_series_path_priors(factor_names, marginal)

    latest_observations = {
        "sp500_price_latest": _monthly_latest(sp500_price, sources.FRED_SP500),
        "spy_adjusted_close_latest": _monthly_latest(sp500_total_return, sources.YAHOO_SPY),
        "btc_close_latest": _monthly_latest(btc_price, sources.YAHOO_BTC),
        "eth_close_latest": _monthly_latest(eth_price, sources.YAHOO_ETH),
        "zillow_home_value_latest_by_factor": {
            home_factor_wire_id[loc]: {
                **_monthly_latest(series, sources.ZILLOW_ZHVI),
                "region_name": ZILLOW_HOME_VALUE_REGIONS[loc][0],
                "state": ZILLOW_HOME_VALUE_REGIONS[loc][1],
            }
            for loc, series in home_values.items()
        },
        "zillow_rent_latest_by_factor": {
            rent_factor_wire_id[loc]: {
                **_monthly_latest(series, sources.ZILLOW_ZORI),
                "region_name": ZILLOW_RENT_REGIONS[loc][0],
                "state": ZILLOW_RENT_REGIONS[loc][1],
            }
            for loc, series in rents.items()
        },
        "case_shiller_sf_latest": _monthly_latest(case_shiller, sources.FRED_SFXRSA),
        "cpi_latest": _monthly_latest(cpi, sources.FRED_CPI),
        "mortgage30_latest": {
            "date": mortgage30["date"].to_list()[-1].isoformat(),
            "value": float(mortgage30["value"].to_list()[-1]),
            "source": sources.FRED_MORTGAGE30.provenance_label,
        },
        "spy_adjusted_close_monthly_return_count": len(marginal[SP500_KEY.wire_id].log_returns),
        "housing_return_sources": {
            "zillow_city_zhvi_by_factor": {
                home_factor_wire_id[loc]: {
                    **_return_frame_summary(
                        returns, source=sources.ZILLOW_ZHVI.provenance_label, used_as_marginal_evidence=True
                    ),
                    "region_name": ZILLOW_HOME_VALUE_REGIONS[loc][0],
                    "state": ZILLOW_HOME_VALUE_REGIONS[loc][1],
                }
                for loc, returns in home_value_returns.items()
            },
            "case_shiller_sf_metro": _return_frame_summary(
                case_shiller_returns, source=sources.FRED_SFXRSA.provenance_label, used_as_marginal_evidence=False
            ),
            "fhfa_sf_oakland_berkeley": _return_frame_summary(
                fhfa_returns, source=sources.FRED_FHFA_SF.provenance_label, used_as_marginal_evidence=False
            ),
        },
        "rent_return_sources": {
            "zillow_city_zori_by_factor": {
                rent_factor_wire_id[loc]: {
                    **_return_frame_summary(
                        returns, source=sources.ZILLOW_ZORI.provenance_label, used_as_marginal_evidence=True
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
        monthly_log_returns=aligned.select(list(factor_names)).to_numpy().astype("float64"),
        monthly_return_months=tuple(aligned["month"].dt.strftime("%Y-%m").to_list()),
        marginal_returns=marginal,
        series_path_calibration=series_path_calibration,
        calibrated_series_path_priors=calibrated_series_path_priors,
        current_mortgage30_rate_pct=float(mortgage30["value"].to_list()[-1]),
        latest_observations=latest_observations,
    )


# Macro level wire id -> the absolute series anchored against it. Home-value series are absent:
# they are not anchored against today.
_ABSOLUTE_LEVEL_SOURCES: dict[str, sources.EvidenceSource] = {
    SP500_KEY.wire_id: sources.FRED_SP500,
    "inflation": sources.FRED_CPI,
    BTC_KEY.wire_id: sources.YAHOO_BTC,
    ETH_KEY.wire_id: sources.YAHOO_ETH,
    "rent:san_francisco_ca": sources.FRED_SF_RENT_CPI,
}


def load_absolute_monthly_levels(wire_ids: Collection[str]) -> dict[str, list[MonthlyLevel]]:
    """Absolute monthly level series (last observation per calendar month, oldest first) for
    each requested macro level wire id, on its real published scale.

    These read the same source files the exogenous evidence fits against, but at their
    absolute level rather than as log-returns, so calibration can anchor a sampled path's
    month 0 to the real spot. Raises `KeyError` for a wire id with no absolute source
    (e.g. home-value series, which are not anchored against today)."""
    out: dict[str, list[MonthlyLevel]] = {}
    for wire in wire_ids:
        if wire not in _ABSOLUTE_LEVEL_SOURCES:
            raise KeyError(f"no absolute level series for level wire id {wire!r}")
        out[wire] = loading.read_monthly_levels(loading.evidence_dir_from_env(), _ABSOLUTE_LEVEL_SOURCES[wire])
    return out
