"""Load aligned monthly log-returns for the exogenous series.

`load_evidence(...)` returns a typed `(HistoricalSeries, ExogenousEvidence)`
tuple from the public Yahoo-SPY, Zillow, and FRED source data (paths are
constants in `evidence_data`). Source-data errors propagate by default.
Callers that intentionally want lower-fidelity FRED-only synthesised evidence
must opt in with `fred_only=True`.

`load_historical(...)` is a thin wrapper for the metric harness, which
only needs `HistoricalSeries`.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from finance.augur.fit.evidence_data import (
    ZILLOW_HOME_VALUE_REGIONS,
    ExogenousEvidence,
    _align_inner,
    _monthly_latest,
    _one_month_returns_from_aligned_levels,
    load_exogenous_evidence,
)
from finance.augur.model.conditioning import ExogenousObservedPoint, ObservationTreatment, ObservationUnits
from finance.augur.model.evidence import EvidenceMetadata, EvidenceMode
from finance.augur.model.path_models.scenarios import DatedHistoricalSeries, HistoricalSeries
from finance.augur.model.series import SP500_KEY, HomeValueKey, InflationKey, LevelSeriesKey, LocationId, RentKey
from finance.evidence.loading import evidence_dir_from_env, monthly_last, read_fred_series, require_positive_levels
from finance.evidence.sources import FRED_CPI, FRED_MORTGAGE30, FRED_SF_RENT_CPI, FRED_SFXRSA, FRED_SP500


def load_evidence(*, fred_only: bool = False) -> tuple[HistoricalSeries, ExogenousEvidence]:
    """Load the full `ExogenousEvidence` and a derived `HistoricalSeries`.

    The default path loads the public Yahoo+Zillow+FRED exogenous evidence and
    lets unreadable source data raise. `fred_only=True` is an explicit
    lower-fidelity fixture/degraded mode; its evidence metadata is labelled as
    synthesized.
    """
    evidence = _evidence_fred_only() if fred_only else load_exogenous_evidence()
    return _historical_from_evidence(evidence), evidence


def load_evidence_segments(*, fred_only: bool = False) -> tuple[DatedHistoricalSeries, ExogenousEvidence]:
    """Load actual aligned level observations without implying continuity across gaps.

    This is the fit boundary for models that explicitly handle separate calendar segments.
    Callers that require one uninterrupted `HistoricalSeries` should keep using `load_evidence`.
    """
    evidence = _evidence_fred_only() if fred_only else load_exogenous_evidence()
    return _dated_history_from_evidence(evidence), evidence


def load_historical(*, fred_only: bool = False) -> HistoricalSeries:
    return load_evidence(fred_only=fred_only)[0]


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


def _historical_from_evidence(evidence: ExogenousEvidence) -> HistoricalSeries:
    history = _dated_history_from_evidence(evidence)
    if len(history.segments) != 1:
        gaps = ", ".join(f"{gap.before_month}→{gap.after_month}" for gap in history.gaps)
        raise ValueError(f"aligned exogenous level history has calendar gaps ({gaps}); use load_evidence_segments")
    segment = history.segments[0]
    if len(segment.months) < 2:
        raise ValueError("aligned exogenous history needs at least two monthly level observations")
    return HistoricalSeries(series_names=history.series_names, levels=segment.levels, months=segment.months)


def _evidence_fred_only() -> ExogenousEvidence:
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
    for key in series_names:
        require_positive_levels(aligned.select(key.wire_id).rename({key.wire_id: "value"}), series_name=key.wire_id)

    level_months = tuple(aligned["month"].dt.strftime("%Y-%m").to_list())
    level_values = aligned.select([key.wire_id for key in series_names]).to_numpy().astype("float64")
    return_months, monthly_log_returns = _one_month_returns_from_aligned_levels(level_months, level_values)
    if len(level_months) < 36:
        raise ValueError(f"only {len(level_months)} aligned months across the FRED-only synthesized series")

    latest_observations: dict[LevelSeriesKey, ExogenousObservedPoint] = {
        SP500_KEY: _monthly_latest(sp500, FRED_SP500, units=ObservationUnits.INDEX_POINTS),
        **{key: _monthly_latest(home, FRED_SFXRSA, units=ObservationUnits.INDEX_POINTS) for key in home_series_keys},
        RentKey(location_id=LocationId("san_francisco_ca")): _monthly_latest(
            rent, FRED_SF_RENT_CPI, units=ObservationUnits.INDEX_POINTS
        ),
        InflationKey(): _monthly_latest(cpi, FRED_CPI, units=ObservationUnits.INDEX_POINTS),
    }
    metadata = EvidenceMetadata(
        auxiliary_observations={
            "mortgage30": ExogenousObservedPoint(
                observed_at=mortgage["date"].to_list()[-1],
                value=float(mortgage["value"].to_list()[-1]),
                units=ObservationUnits.PERCENT,
                source_id=f"public:{FRED_MORTGAGE30.provenance_label}",
                treatment=ObservationTreatment.INFORMATIVE,
            )
        },
        mode=EvidenceMode(
            mode="fred_only_synthesized",
            explicit=True,
            description="FRED-only synthesized evidence explicitly selected; Yahoo SPY and Zillow ZHVI were not loaded.",
        ),
    )
    return ExogenousEvidence(
        series_names=series_names,
        monthly_level_values=level_values,
        monthly_level_months=level_months,
        monthly_log_returns=monthly_log_returns,
        monthly_return_months=return_months,
        current_mortgage30_rate_pct=float(mortgage["value"].to_list()[-1]),
        latest_observations=latest_observations,
        metadata=metadata,
    )


def _dated_history_from_evidence(evidence: ExogenousEvidence) -> DatedHistoricalSeries:
    return DatedHistoricalSeries(
        series_names=evidence.series_names,
        levels=np.asarray(evidence.monthly_level_values, dtype=np.float64),
        months=evidence.monthly_level_months,
    )


def aligned_as_of_observations(evidence: ExogenousEvidence) -> dict[LevelSeriesKey, ExogenousObservedPoint]:
    """Absolute factor anchors from the final common observed level month.

    Per-source latest observations can be newer than the common fitted history and can have
    different months. A fitted VECM must use one shared state date, so its runtime anchors are
    selected from the exact aligned level row that supplies the model terminal state.
    """
    if not evidence.monthly_level_months:
        raise ValueError("aligned exogenous level history is empty")
    as_of = date.fromisoformat(f"{evidence.monthly_level_months[-1]}-01")
    levels = np.asarray(evidence.monthly_level_values, dtype=np.float64)
    if levels.shape != (len(evidence.monthly_level_months), len(evidence.series_names)):
        raise ValueError("aligned exogenous levels do not match their calendar or factor dimensions")
    return {
        factor: evidence.latest_observations[factor].model_copy(
            update={"observed_at": as_of, "value": float(levels[-1, index])}
        )
        for index, factor in enumerate(evidence.series_names)
    }
