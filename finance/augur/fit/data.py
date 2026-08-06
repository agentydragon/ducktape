"""Load aligned monthly log-returns for the exogenous series.

`load_evidence(...)` returns a typed `(HistoricalSeries, ExogenousEvidence)`
tuple from the public Yahoo-SPY, Zillow, and FRED source data (paths are
constants in `evidence_data`). Source-data errors propagate by default.
Callers that intentionally want lower-fidelity FRED-only synthesised evidence
must opt in with `fred_only=True` or `load_fred_only_evidence(...)`.

`load_historical(...)` is a thin wrapper for the metric harness, which
only needs `HistoricalSeries`.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from finance.augur.fit.evidence_data import (
    ZILLOW_HOME_VALUE_REGIONS,
    ExogenousEvidence,
    PeriodReturns,
    _align_inner,
    _monthly_latest,
    calibrate_series_path_priors,
    load_exogenous_evidence,
)
from finance.augur.model.path_models.scenarios import HistoricalSeries
from finance.augur.model.series import SP500_KEY, HomeValueKey, InflationKey, LevelSeriesKey, LocationId, RentKey
from finance.evidence.loading import evidence_dir_from_env, monthly_last, read_fred_series
from finance.evidence.sources import FRED_CPI, FRED_MORTGAGE30, FRED_SF_RENT_CPI, FRED_SFXRSA, FRED_SP500


def load_evidence(*, fred_only: bool = False) -> tuple[HistoricalSeries, ExogenousEvidence]:
    """Load the full `ExogenousEvidence` and a derived `HistoricalSeries`.

    The default path loads the public Yahoo+Zillow+FRED exogenous evidence and
    lets unreadable source data raise. `fred_only=True` is an explicit
    lower-fidelity fixture/degraded mode; its evidence metadata is labelled as
    synthesized.
    """
    if fred_only:
        return _evidence_fred_only()
    evidence = load_exogenous_evidence()
    return _historical_from_evidence(evidence), evidence


def load_fred_only_evidence() -> tuple[HistoricalSeries, ExogenousEvidence]:
    """Load explicitly selected FRED-only synthesized exogenous evidence."""
    return load_evidence(fred_only=True)


def load_historical(*, fred_only: bool = False) -> HistoricalSeries:
    return load_evidence(fred_only=fred_only)[0]


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


def _historical_from_evidence(evidence: ExogenousEvidence) -> HistoricalSeries:
    return _historical_from_log_returns(
        evidence.series_names, evidence.monthly_log_returns, evidence.monthly_return_months
    )


def _evidence_fred_only() -> tuple[HistoricalSeries, ExogenousEvidence]:
    """Read only FRED CSVs (no Yahoo, no Zillow) and synthesise a
    `ExogenousEvidence` matching the production loader's shape with what we
    can construct: SP500 from FRED price-level (no dividends), Case-Shiller
    SF for housing, FRED rent CPI, FRED US CPI, FRED 30-year mortgage."""
    # Home-value series are derived structurally from the configured locations (each one's
    # HomeValueKey wire id); the FRED-only path replicates one Case-Shiller SF series across them.
    home_series_keys = tuple(HomeValueKey(location_id=loc) for loc in ZILLOW_HOME_VALUE_REGIONS)
    series_names: tuple[LevelSeriesKey, ...] = (
        SP500_KEY,
        *home_series_keys,
        RentKey(location_id=LocationId("san_francisco_ca")),
        InflationKey(),
    )
    sp500 = monthly_last(read_fred_series(evidence_dir_from_env(), FRED_SP500))
    home = monthly_last(read_fred_series(evidence_dir_from_env(), FRED_SFXRSA))
    rent = monthly_last(read_fred_series(evidence_dir_from_env(), FRED_SF_RENT_CPI))
    cpi = monthly_last(read_fred_series(evidence_dir_from_env(), FRED_CPI))
    mortgage = read_fred_series(evidence_dir_from_env(), FRED_MORTGAGE30)

    aligned = _align_inner(
        {
            SP500_KEY: sp500,
            **dict.fromkeys(home_series_keys, home),
            RentKey(location_id=LocationId("san_francisco_ca")): rent,
            InflationKey(): cpi,
        },
        value_column="value",
    )
    if aligned.height < 36:
        raise ValueError(f"only {aligned.height} aligned months across the FRED-only synthesized series")

    monthly_log_returns = np.diff(
        np.log(aligned.select([key.wire_id for key in series_names]).to_numpy().astype("float64")), axis=0
    )
    return_months = tuple(aligned["month"].dt.strftime("%Y-%m").to_list()[1:])
    historical = _historical_from_log_returns(series_names, monthly_log_returns, return_months)

    durations = np.ones_like(monthly_log_returns[:, 0])
    marginal: dict[LevelSeriesKey, PeriodReturns] = {
        key: PeriodReturns(log_returns=monthly_log_returns[:, idx], duration_months=durations)
        for idx, key in enumerate(series_names)
    }
    series_path_calibration, calibrated_series_path_priors = calibrate_series_path_priors(series_names, marginal)
    latest_observations: dict[str, Any] = {
        "sp500_price_latest": _monthly_latest(sp500, FRED_SP500),
        "case_shiller_sf_latest": _monthly_latest(home, FRED_SFXRSA),
        "case_shiller_home_value_latest_by_factor": {
            # A serialized provenance blob, so its keys are wire ids by nature.
            key.wire_id: _monthly_latest(home, FRED_SFXRSA)
            for key in home_series_keys
        },
        "sf_rent_cpi_latest": _monthly_latest(rent, FRED_SF_RENT_CPI),
        "cpi_latest": _monthly_latest(cpi, FRED_CPI),
        "mortgage30_latest": {
            "date": mortgage["date"].to_list()[-1].isoformat(),
            "value": float(mortgage["value"].to_list()[-1]),
            "source": FRED_MORTGAGE30.provenance_label,
        },
        "evidence_mode": {
            "mode": "fred_only_synthesized",
            "explicit": True,
            "description": "FRED-only synthesized evidence explicitly selected; Yahoo SPY and Zillow ZHVI were not loaded.",
        },
    }
    evidence = ExogenousEvidence(
        series_names=series_names,
        monthly_log_returns=monthly_log_returns,
        monthly_return_months=return_months,
        marginal_returns=marginal,
        series_path_calibration=series_path_calibration,
        calibrated_series_path_priors=calibrated_series_path_priors,
        current_mortgage30_rate_pct=float(mortgage["value"].to_list()[-1]),
        latest_observations=latest_observations,
    )
    return historical, evidence


def _historical_from_log_returns(
    series_names: tuple[LevelSeriesKey, ...], monthly_log_returns: np.ndarray, return_months: tuple[str, ...]
) -> HistoricalSeries:
    n_factors = monthly_log_returns.shape[1]
    cum = np.concatenate([np.zeros((1, n_factors)), np.cumsum(monthly_log_returns, axis=0)], axis=0)
    levels = np.exp(cum)
    # The history's month-0 label is the month before the first return month (return_months are
    # "YYYY-MM", oldest first).
    year, month = map(int, return_months[0].split("-"))
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    months = (f"{prev_year:04d}-{prev_month:02d}", *return_months)
    return HistoricalSeries(series_names=series_names, levels=levels, months=months)
