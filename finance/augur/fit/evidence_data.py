from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

import numpy as np
import polars as pl

from finance.augur.model.conditioning import ExogenousObservedPoint, ObservationTreatment, ObservationUnits
from finance.augur.model.series import (
    SP500_KEY,
    HomeValueKey,
    InflationKey,
    LevelSeriesKey,
    LocationId,
    RentKey,
    SecurityKey,
    SecuritySymbol,
)
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

# Minimum overlapping months required across the aligned exogenous series.
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


@dataclass(frozen=True)
class ExogenousEvidence:
    """Observed monthly evidence, keyed by the typed series it describes.

    `LevelSeriesKey`, not wire-id strings: this layer BUILDS the typed keys
    (`HomeValueKey(location_id=…)`), and its consumer used to flatten them to strings here
    and parse them straight back at `fit/data.py`. The typed value existed at both ends and
    only the middle was stringly typed, which bought nothing and cost the `parse` round trip.
    """

    series_names: tuple[LevelSeriesKey, ...]
    monthly_level_values: np.ndarray
    monthly_level_months: tuple[str, ...]
    monthly_log_returns: np.ndarray
    monthly_return_months: tuple[str, ...]
    current_mortgage30_rate_pct: float
    latest_observations: dict[LevelSeriesKey, ExogenousObservedPoint]


# Evidence series are carried as 2-column polars frames: raw series as `(date, value)`, and the
# monthly resamplings/returns below as `(month, ...)` where `month` is the first day of the month.


def _align_inner(frames: dict[LevelSeriesKey, pl.DataFrame], value_column: str) -> pl.DataFrame:
    """Inner-join `{key: (month, <value_column>)}` frames on month, one column per series.

    The polars column name is the wire id — a frame column is a string by construction; the
    KEY is what carries identity, and it stays typed on both sides of this call."""
    return loading.align_monthly_frames(
        {key.wire_id: frame for key, frame in frames.items()}, value_column=value_column
    )


def _one_month_returns_from_aligned_levels(
    months: tuple[str, ...], level_values: np.ndarray
) -> tuple[tuple[str, ...], np.ndarray]:
    """Derive returns only where adjacent observed level rows are adjacent calendar months."""
    if level_values.ndim != 2 or level_values.shape[0] != len(months):
        raise ValueError("aligned level values and their calendar labels have different shapes")
    if not np.all(np.isfinite(level_values)) or not np.all(level_values > 0):
        raise ValueError("aligned level values must contain only finite positive levels")
    month_indices = tuple(int(month[:4]) * 12 + int(month[5:7]) for month in months)
    adjacent = np.diff(np.asarray(month_indices, dtype=np.int64)) == 1
    if not np.any(adjacent):
        raise ValueError("aligned level evidence has no consecutive monthly transitions")
    returns = np.diff(np.log(level_values), axis=0)[adjacent]
    return tuple(month for month, keep in zip(months[1:], adjacent, strict=True) if keep), returns


def _monthly_latest(
    monthly: pl.DataFrame,
    source: sources.EvidenceSource,
    *,
    units: ObservationUnits,
    region_name: str | None = None,
    region_state: str | None = None,
) -> ExogenousObservedPoint:
    # Every value here becomes a current macro-level anchor. Rates (which may be zero or
    # negative) use their separate PERCENT observation path and do not pass through this helper.
    loading.require_positive_levels(monthly.tail(1), series_name=source.provenance_label)
    return ExogenousObservedPoint(
        observed_at=monthly["month"].to_list()[-1],
        value=float(monthly["value"].to_list()[-1]),
        units=units,
        source_id=f"public:{source.provenance_label}",
        treatment=ObservationTreatment.HARD_START,
        region_name=region_name,
        region_state=region_state,
    )


def load_exogenous_evidence() -> ExogenousEvidence:
    # SPY is daily; require thousands of rows to catch a truncated file.
    sp500_total_return = loading.monthly_last(_yahoo_frame(sources.YAHOO_SPY, minimum_samples=1000))
    btc_price = loading.monthly_last(_yahoo_frame(sources.YAHOO_BTC))
    eth_price = loading.monthly_last(_yahoo_frame(sources.YAHOO_ETH))
    cpi = loading.monthly_last(_fred_frame(sources.FRED_CPI))
    mortgage30 = _fred_frame(sources.FRED_MORTGAGE30)
    # Home-value and rent evidence stay keyed by LocationId (their role-natural key); the
    # flat series wire ids (HomeValueKey/RentKey .wire_id) are derived only at the matrix/JSON
    # boundaries below.
    home_table = loading.zillow_city_table(_source_bytes(sources.ZILLOW_ZHVI), sources.ZILLOW_ZHVI)
    rent_table = loading.zillow_city_table(_source_bytes(sources.ZILLOW_ZORI), sources.ZILLOW_ZORI)
    home_values = {
        location_id: loading.zillow_city_series_frame(
            home_table, sources.ZILLOW_ZHVI, region_name=region_name, state=state
        )
        for location_id, (region_name, state) in ZILLOW_HOME_VALUE_REGIONS.items()
    }
    rents = {
        location_id: loading.zillow_city_series_frame(
            rent_table, sources.ZILLOW_ZORI, region_name=region_name, state=state
        )
        for location_id, (region_name, state) in ZILLOW_RENT_REGIONS.items()
    }
    home_series_key = {location_id: HomeValueKey(location_id=location_id) for location_id in home_values}
    rent_series_key = {location_id: RentKey(location_id=location_id) for location_id in rents}
    series_names: tuple[LevelSeriesKey, ...] = (
        SP500_KEY,
        BTC_KEY,
        ETH_KEY,
        *home_series_key.values(),
        *rent_series_key.values(),
        InflationKey(),
    )
    aligned_levels = _align_inner(
        {
            SP500_KEY: sp500_total_return,
            BTC_KEY: btc_price,
            ETH_KEY: eth_price,
            **{home_series_key[loc]: series for loc, series in home_values.items()},
            **{rent_series_key[loc]: series for loc, series in rents.items()},
            InflationKey(): cpi,
        },
        value_column="value",
    )
    monthly_level_months = tuple(aligned_levels["month"].dt.strftime("%Y-%m").to_list())
    monthly_level_values = aligned_levels.select([key.wire_id for key in series_names]).to_numpy().astype("float64")
    monthly_return_months, monthly_log_returns = _one_month_returns_from_aligned_levels(
        monthly_level_months, monthly_level_values
    )
    if len(monthly_return_months) < MINIMUM_ALIGNED_MONTHS:
        raise ValueError(f"only {len(monthly_return_months)} aligned exogenous months were available")

    latest_observations: dict[LevelSeriesKey, ExogenousObservedPoint] = {
        SP500_KEY: _monthly_latest(sp500_total_return, sources.YAHOO_SPY, units=ObservationUnits.USD_PER_UNIT),
        BTC_KEY: _monthly_latest(btc_price, sources.YAHOO_BTC, units=ObservationUnits.USD_PER_UNIT),
        ETH_KEY: _monthly_latest(eth_price, sources.YAHOO_ETH, units=ObservationUnits.USD_PER_UNIT),
        **{
            home_series_key[loc]: _monthly_latest(
                series,
                sources.ZILLOW_ZHVI,
                units=ObservationUnits.USD,
                region_name=ZILLOW_HOME_VALUE_REGIONS[loc][0],
                region_state=ZILLOW_HOME_VALUE_REGIONS[loc][1],
            )
            for loc, series in home_values.items()
        },
        **{
            rent_series_key[loc]: _monthly_latest(
                series,
                sources.ZILLOW_ZORI,
                units=ObservationUnits.USD_PER_MONTH,
                region_name=ZILLOW_RENT_REGIONS[loc][0],
                region_state=ZILLOW_RENT_REGIONS[loc][1],
            )
            for loc, series in rents.items()
        },
        InflationKey(): _monthly_latest(cpi, sources.FRED_CPI, units=ObservationUnits.INDEX_POINTS),
    }
    return ExogenousEvidence(
        series_names=series_names,
        monthly_level_values=monthly_level_values,
        monthly_level_months=monthly_level_months,
        monthly_log_returns=monthly_log_returns,
        monthly_return_months=monthly_return_months,
        current_mortgage30_rate_pct=float(mortgage30["value"].to_list()[-1]),
        latest_observations=latest_observations,
    )


# Macro level series -> the absolute evidence source anchored against it. Home-value series are absent:
# they are not anchored against today.
_ABSOLUTE_LEVEL_SOURCES: dict[LevelSeriesKey, sources.EvidenceSource] = {
    SP500_KEY: sources.FRED_SP500,
    InflationKey(): sources.FRED_CPI,
    BTC_KEY: sources.YAHOO_BTC,
    ETH_KEY: sources.YAHOO_ETH,
    RentKey(location_id=LocationId("san_francisco_ca")): sources.FRED_SF_RENT_CPI,
}


def load_absolute_monthly_levels(keys: Collection[LevelSeriesKey]) -> dict[LevelSeriesKey, list[MonthlyLevel]]:
    """Absolute monthly level series (last observation per calendar month, oldest first) for
    each requested macro level series, on its real published scale.

    These read the same source files the exogenous evidence fits against, but at their
    absolute level rather than as log-returns, so calibration can anchor a sampled path's
    month 0 to the real spot. Raises `KeyError` for a series with no absolute source
    (e.g. home-value series, which are not anchored against today)."""
    out: dict[LevelSeriesKey, list[MonthlyLevel]] = {}
    for key in keys:
        if key not in _ABSOLUTE_LEVEL_SOURCES:
            raise KeyError(f"no absolute level series for {key.wire_id!r}")
        out[key] = loading.read_monthly_levels(loading.evidence_dir_from_env(), _ABSOLUTE_LEVEL_SOURCES[key])
    return out
