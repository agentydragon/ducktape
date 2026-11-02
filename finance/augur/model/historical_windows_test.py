"""Tests for the historical-window sampler.

Synthetic histories with a KNOWN shape, so an assertion names a property of the replay rather
than a fact about FRED. A test against the real record would assert that the past equals the
past, and would break on every evidence refresh while catching nothing.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.bond_fund import BondFundSpec, YieldCurve
from finance.augur.model.equity import EquitySpec
from finance.augur.model.historical_windows import (
    HistoricalWindowsModel,
    HistoricalWindowsProviderConfig,
    MacroHistory,
    load_ex_us_equity_levels,
    load_macro_history,
    macro_history_from_frames,
    splice_monthly_frames,
)
from finance.augur.model.series import (
    InflationKey,
    LevelSeriesKey,
    SecurityDistributionKey,
    SecurityKey,
    SecuritySymbol,
)
from finance.evidence import loading, sources
from finance.evidence.snapshot import EvidenceSnapshot

BOND = SecuritySymbol("BOND")
CASH = SecuritySymbol("CASH")
EQUITY = SecuritySymbol("EQ")
MONTHS = 600


def _month_seq(count: int, start_year: int = 1900) -> list[date]:
    return [date(start_year + index // 12, index % 12 + 1, 1) for index in range(count)]


_HISTORY_START_YEAR = 1970


def _history(months: int = MONTHS) -> MacroHistory:
    """A record whose every series is strictly increasing, so a window's identity is visible in
    its values: window `i` starts at exactly the month-`i` level of each series."""

    index = np.arange(months, dtype=np.float64)
    # Equity and CPI grow at an ACCELERATING rate, not a constant one. A constant-growth series
    # is shape-invariant under rebasing, so every window would replay identically and the tests
    # that distinguish windows would pass against a sampler that always returned window zero.
    return MacroHistory(
        months=tuple(_month_seq(months, _HISTORY_START_YEAR)),
        short_rate=0.01 + index * 0.0001,
        term_spread=0.005 + index * 0.00001,
        # Corporate curves sit above the government one and are distinguishable from it and
        # from each other, so a test can tell which curve an instrument actually priced off.
        corporate_aaa_yield=0.02 + index * 0.0001,
        corporate_baa_yield=0.03 + index * 0.0001,
        equity_level=100.0 * np.exp(np.cumsum(0.004 + index * 0.00001)),
        cpi_level=100.0 * np.exp(np.cumsum(0.0015 + index * 0.000003)),
    )


def _model(history: MacroHistory | None = None) -> HistoricalWindowsModel:
    return HistoricalWindowsModel(
        history=history if history is not None else _history(),
        instruments=(
            BondFundSpec(symbol=BOND, maturity_years=6.0, initial_price_usd=100.0),
            BondFundSpec(symbol=CASH, maturity_years=0.0, initial_price_usd=1.0),
        ),
        equity=EquitySpec(symbol=EQUITY, initial_price_usd=500.0),
    )


def _series(model: HistoricalWindowsModel, key: LevelSeriesKey, *, horizon: int, rollouts: int) -> np.ndarray:
    bundle = model.materialize(window_starts=model.window_starts(horizon)[:rollouts], horizon_months=horizon)
    return bundle.level_matrix(key, rollout_count=rollouts, horizon_months=horizon)


def test_each_rollout_replays_a_different_window() -> None:
    """The whole idea: rollout `i` is a different slice of the record, not a different draw."""

    equity = _series(_model(), SecurityKey(symbol=EQUITY), horizon=120, rollouts=4)

    # Rebased to a common start, so what distinguishes windows is their SHAPE.
    assert np.all(equity[:, 0] == 500.0)
    assert len({round(float(row[-1]), 6) for row in equity}) == 4


def test_in_memory_replay_needs_no_fitted_artifact() -> None:
    history = _history(months=13)
    with patch.object(Path, "read_text", side_effect=AssertionError("replay must not read fitted artifacts")):
        model = _model(history)
        equity = _series(model, SecurityKey(symbol=EQUITY), horizon=12, rollouts=1)

    np.testing.assert_array_equal(equity[0], 500.0 * (history.equity_level / history.equity_level[0]))


@pytest.mark.parametrize("selection", [(2, 0, 1), (1,), (0, 2), (0, 1, 2, 3)])
def test_named_windows_preserve_every_series_under_reordering_partition_and_extension(
    selection: tuple[int, ...],
) -> None:
    model = _model()
    horizon = 120
    dates = (date(2000, 3, 1), date(1970, 1, 1), date(1985, 8, 1), date(1990, 4, 1))
    baseline = model.materialize(window_starts=dates[:3], horizon_months=horizon)
    selected = model.materialize(window_starts=tuple(dates[index] for index in selection), horizon_months=horizon)

    for key in baseline.levels.series_keys():
        before = baseline.level_matrix(key, rollout_count=3, horizon_months=horizon)
        after = selected.level_matrix(key, rollout_count=len(selection), horizon_months=horizon)
        for output_index, original_index in enumerate(selection):
            if original_index < 3:
                np.testing.assert_array_equal(after[output_index], before[original_index])


@pytest.mark.parametrize(
    ("starts", "message"),
    [
        ((), "at least one"),
        ((date(1970, 1, 1), date(1970, 1, 1)), "must not repeat"),
        ((date(1969, 12, 1),), "unavailable"),
        ((date(1970, 1, 2),), "unavailable"),
        ((date(2010, 1, 1),), "unavailable"),
    ],
)
def test_invalid_window_selections_are_rejected(starts: tuple[date, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _model().materialize(window_starts=starts, horizon_months=120)


def test_a_horizon_longer_than_the_record_is_rejected() -> None:
    model = _model(_history(months=100))
    with pytest.raises(ValueError, match="too few for a"):
        model.materialize(window_starts=(date(1970, 1, 1),), horizon_months=120)


def test_negative_horizons_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _model().materialize(window_starts=(date(1970, 1, 1),), horizon_months=-1)


def test_zero_horizon_can_select_the_records_final_month() -> None:
    model = _model()
    bundle = model.materialize(window_starts=(model.history.months[-1],), horizon_months=0)
    np.testing.assert_array_equal(bundle.level_matrix(InflationKey(), rollout_count=1, horizon_months=0), [[100.0]])


def test_the_independent_window_estimate_is_reported_and_is_tiny() -> None:
    """The number that bounds every conclusion drawn from this provider, and the reason it is a
    method rather than a comment: 46 years of monthly data admit ~199 overlapping 30-year
    windows and about 1.5 independent ones."""

    model = _model(_history(months=559))

    assert model.window_count(360) == 199
    assert model.independent_window_estimate(360) == pytest.approx(1.55, abs=0.01)


def test_gap_safe_window_estimate_is_documented_as_a_run_length_approximation() -> None:
    history = _history(months=559)
    gapped = history._rows(np.asarray([i for i in range(len(history.months)) if i != 250]))
    model = _model(gapped)

    assert model.independent_window_estimate(360) == 0.0
    assert model.window_count(360) == 0
    assert _model(_history(months=360)).independent_window_estimate(360) == 0.0


@pytest.mark.parametrize("rollouts", [1, 3, 480])
def test_explicit_selection_preserves_the_previous_evenly_spaced_windows(rollouts: int) -> None:
    """Preserve the old numerical convention when the author selects that same window set."""
    model = _model()
    horizon = 120
    indices = np.linspace(0, MONTHS - horizon - 1, rollouts).round().astype(int)
    dates = tuple(model.history.months[index] for index in indices)
    bundle = model.materialize(window_starts=dates, horizon_months=horizon)

    for key, record, opening in (
        (SecurityKey(symbol=EQUITY), model.history.equity_level, 500.0),
        (InflationKey(), model.history.cpi_level, 100.0),
    ):
        expected = np.stack([opening * (record[index : index + horizon + 1] / record[index]) for index in indices])
        np.testing.assert_array_equal(
            bundle.level_matrix(key, rollout_count=rollouts, horizon_months=horizon), expected
        )


def test_gap_splits_replay_windows_and_never_bridges_monthly_growth() -> None:
    record = _history(36)
    gap_index = 20
    keep = np.asarray([index for index in range(len(record.months)) if index != gap_index])
    history = record._rows(keep)
    model = HistoricalWindowsModel(history=history)
    horizon = 12

    starts = model.window_starts(horizon)
    # The record has 20 months before the gap and 15 after it: each segment contributes only
    # windows fully contained in that segment.
    assert model.window_count(horizon) == 8 + 3
    assert starts == tuple(history.months[index] for index in (*range(8), *range(20, 23)))

    selected = (starts[7], starts[8])
    paths = model.market_paths(window_starts=selected, horizon_months=horizon)
    assert paths.equity_total_return_index is not None
    for rollout, start in enumerate(selected):
        start_index = history.months.index(start)
        months = history.months[start_index : start_index + horizon + 1]
        assert len(months) == horizon + 1
        assert all(
            later == date(earlier.year + int(earlier.month == 12), earlier.month % 12 + 1, 1)
            for earlier, later in pairwise(months)
        )
        expected = history.equity_level[start_index : start_index + horizon + 1]
        np.testing.assert_array_equal(paths.equity_total_return_index[rollout], expected / expected[0])


def test_the_aligned_record_is_the_intersection_of_all_selected_series() -> None:
    """A total-return equity series is the short one, so it sets where usable history starts.
    Aligning on anything less than all four would put a rate from one month beside an equity
    level from another — invisible in the levels, wrong in every correlation."""

    months = _month_seq(100, 2000)
    history = macro_history_from_frames(
        {
            "short_rate_percent": pl.DataFrame({"month": months, "value": [4.0] * 100}),
            "long_rate_percent": pl.DataFrame({"month": months, "value": [5.0] * 100}),
            "corporate_aaa_percent": pl.DataFrame({"month": months, "value": [6.0] * 100}),
            "corporate_baa_percent": pl.DataFrame({"month": months, "value": [7.0] * 100}),
            "equity_level": pl.DataFrame({"month": months[40:], "value": [100.0 + m for m in range(40, 100)]}),
            "cpi_level": pl.DataFrame({"month": months[20:90], "value": [100.0 + m for m in range(20, 90)]}),
        }
    )

    assert len(history.months) == 50  # months 40..89
    assert history.short_rate[0] == pytest.approx(0.04)
    assert history.term_spread[0] == pytest.approx(0.01)


def test_disjoint_series_are_rejected() -> None:
    months = _month_seq(10, 2000)
    with pytest.raises(ValueError, match="no shared months"):
        macro_history_from_frames(
            {
                "short_rate_percent": pl.DataFrame({"month": months, "value": [4.0] * 10}),
                "long_rate_percent": pl.DataFrame({"month": months, "value": [5.0] * 10}),
                "corporate_aaa_percent": pl.DataFrame({"month": months, "value": [6.0] * 10}),
                "corporate_baa_percent": pl.DataFrame({"month": months, "value": [7.0] * 10}),
                "equity_level": pl.DataFrame({"month": _month_seq(10, 2005), "value": [100.0] * 10}),
                "cpi_level": pl.DataFrame({"month": months, "value": [100.0] * 10}),
            }
        )


def test_mismatched_history_lengths_are_rejected() -> None:
    with pytest.raises(ValueError, match="different lengths"):
        MacroHistory(
            months=tuple(_month_seq(10)),
            short_rate=np.zeros(10),
            term_spread=np.zeros(10),
            corporate_aaa_yield=np.zeros(10),
            corporate_baa_yield=np.zeros(10),
            equity_level=np.ones(9),
            cpi_level=np.ones(10),
        )


def test_the_provenance_says_the_rollouts_are_not_independent() -> None:
    """A percentile over overlapping windows is a count of starting months, not a probability.
    Carried on the bundle so a consumer that logs provenance cannot lose the caveat."""

    model = _model()
    bundle = model.materialize(window_starts=model.window_starts(120)[:4], horizon_months=120)

    assert bundle.provenance["distinct_windows_available"] == 480
    assert bundle.provenance["independent_window_estimate"] == 5.0
    assert bundle.provenance["notes"]


def _level_frame(start_year: int, values: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"month": _month_seq(len(values), start_year), "value": values})


def test_the_splice_shifts_the_early_series_to_meet_the_late_one() -> None:
    """The seam is where continuity matters: a 30-year window starting in 1926 crosses 1953, so
    a step there would read as a real rate move rather than a change of measurement."""

    late = _level_frame(1910, [4.0 + 0.01 * i for i in range(300)])
    # Same shape, offset by a constant, starting 120 months earlier.
    early = _level_frame(1900, [4.0 + 0.01 * (i - 120) + 0.5 for i in range(420)])

    spliced = splice_monthly_frames(early=early, late=late)

    assert spliced.height == 420
    assert spliced["month"][0] == early["month"][0]
    assert spliced["month"][-1] == late["month"][-1]
    # Continuous across the seam: consecutive steps are the series' own 0.01, not 0.01 - 0.5.
    steps = np.diff(spliced["value"].to_numpy())
    assert max(steps) == pytest.approx(0.01, abs=1e-9)
    assert min(steps) == pytest.approx(0.01, abs=1e-9)


def test_the_late_series_is_never_altered_by_the_splice() -> None:
    """It is the better measurement and the one every other fit uses; the early series is what
    bends to meet it. Shifting the modern half would silently change the fitted rate block."""

    late = _level_frame(1910, [4.0 + 0.02 * i for i in range(300)])
    early = _level_frame(1900, [9.0] * 420)

    spliced = splice_monthly_frames(early=early, late=late)

    assert spliced.filter(pl.col("month") >= late["month"][0])["value"].to_list() == late["value"].to_list()


def test_the_shift_comes_from_the_seam_not_the_whole_overlap() -> None:
    """The two series disagree by a time-VARYING amount, so a whole-overlap mean would import a
    late-period discrepancy into an early-period observation."""

    late = _level_frame(1910, [4.0] * 300)
    # Agrees at the seam, then drifts far apart later in the overlap.
    early = _level_frame(1900, [4.0] * 130 + [4.0 + 0.05 * i for i in range(290)])

    spliced = splice_monthly_frames(early=early, late=late)

    # Seam-anchored: the offset is ~0 there, so pre-seam values pass through essentially intact.
    assert spliced["value"][0] == pytest.approx(4.0, abs=0.02)


def test_too_little_overlap_to_anchor_is_rejected() -> None:
    late = _level_frame(1910, [4.0] * 300)
    early = _level_frame(1900, [4.5] * 125)  # only 5 months of overlap
    with pytest.raises(ValueError, match="fewer than the"):
        splice_monthly_frames(early=early, late=late)


def _write_evidence(directory: Path) -> None:
    """A miniature evidence checkout: four files in the real formats, 400 months from 1970."""

    months = _month_seq(400, 1970)

    body = ["This file was created using a test.", "", ",Mkt-RF,SMB,HML,RF"]
    body.extend(f"{m.year}{m.month:02d},   1.00,   0.00,   0.00,   0.30" for m in months)
    body.extend(
        ["", " Annual Factors: January-December ", ",Mkt-RF,SMB,HML,RF", "  1971,  29.44,  -2.20,  -4.58,   3.12"]
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("F-F_Research_Data_Factors.csv", "\n".join(body) + "\n")
    (directory / sources.FRENCH_FACTORS.output_filename).write_bytes(buffer.getvalue())

    def csv(source: object, series_id: str, value_for: object) -> None:
        rows = "\n".join(f"{m.isoformat()},{value_for(m)}" for m in months)  # type: ignore[operator]
        (directory / source.output_filename).write_text(f"observation_date,{series_id}\n{rows}\n")  # type: ignore[attr-defined]

    csv(sources.FRED_GS10, "GS10", lambda m: 5.0)
    csv(sources.FRED_LTGOVTBD, "LTGOVTBD", lambda m: 5.4)
    csv(sources.FRED_CPI_NSA, "CPIAUCNS", lambda m: 100.0)
    csv(sources.FRED_AAA, "AAA", lambda m: 6.0)
    csv(sources.FRED_BAA, "BAA", lambda m: 7.0)


def test_the_macro_record_assembles_the_selected_series_from_evidence(tmp_path: Path) -> None:
    """The seam between selected source values and the frame the replay can use."""

    _write_evidence(tmp_path)
    history = load_macro_history(tmp_path)

    assert len(history.months) == 400
    # French's RF is a monthly simple return; the record speaks annualized decimals.
    assert history.short_rate[0] == pytest.approx(0.30 * 12 / 100.0)
    # Mkt-RF + RF = 1.30%/month, compounded into an index.
    assert history.equity_level[1] / history.equity_level[0] == pytest.approx(1.013)
    # GS10 5.0 against a short rate of 3.6%.
    assert history.term_spread[0] == pytest.approx(0.05 - 0.036)


def test_each_source_is_loaded_once_and_the_shared_french_frame_supplies_both_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_evidence(tmp_path)
    real_source_bytes = EvidenceSnapshot.source_bytes
    reads: list[sources.EvidenceSource] = []

    def count_source_bytes(snapshot: EvidenceSnapshot, source: sources.EvidenceSource) -> bytes:
        reads.append(source)
        return real_source_bytes(snapshot, source)

    monkeypatch.setattr(EvidenceSnapshot, "source_bytes", count_source_bytes)
    history = load_macro_history(tmp_path)

    assert set(reads) == {
        sources.FRENCH_FACTORS,
        sources.FRED_LTGOVTBD,
        sources.FRED_GS10,
        sources.FRED_CPI_NSA,
        sources.FRED_AAA,
        sources.FRED_BAA,
    }
    assert len(reads) == len(set(reads))
    assert history.short_rate[0] == pytest.approx(0.30 * 12 / 100.0)
    assert history.equity_level[1] / history.equity_level[0] == pytest.approx(1.013)


def test_joint_macro_history_keeps_a_gap_in_a_required_series() -> None:
    months = _month_seq(4, 1970)

    history = macro_history_from_frames(
        {
            name: pl.DataFrame(
                {"month": months if name != "corporate_baa_percent" else months[:2] + months[3:], "value": values}
            )
            for name, values in (
                ("short_rate_percent", [1.0] * 4),
                ("long_rate_percent", [1.0] * 4),
                ("corporate_aaa_percent", [1.0] * 4),
                ("corporate_baa_percent", [1.0] * 3),
                ("equity_level", [1.0] * 4),
                ("cpi_level", [1.0] * 4),
            )
        }
    )

    assert history.months == (months[0], months[1], months[3])


def test_the_annual_factor_rows_do_not_reach_the_record(tmp_path: Path) -> None:
    """End-to-end version of the loader's own guard: a 29.44% monthly equity return would be
    the single largest observation in the record and would survive every downstream check."""

    _write_evidence(tmp_path)
    history = load_macro_history(tmp_path)

    growth = history.equity_level[1:] / history.equity_level[:-1]
    assert float(np.max(growth)) == pytest.approx(1.013)


def test_experiment_config_loads_history_and_materializes_the_chosen_products(tmp_path: Path) -> None:
    _write_evidence(tmp_path)
    config = HistoricalWindowsProviderConfig.model_validate(
        {
            "type": "historical_windows",
            "evidence_dir": str(tmp_path),
            "equity": {"symbol": "VOO", "initial_price_usd": 520.0},
            "instruments": [{"symbol": "CMF", "maturity_years": 5.5, "spread": -0.012}],
        }
    )

    model = config.realize_model()
    bundle = model.materialize(window_starts=(date(1975, 1, 1),), horizon_months=12)
    assert bundle.levels.series_keys() == {
        InflationKey(),
        SecurityKey(symbol=SecuritySymbol("VOO")),
        SecurityKey(symbol=SecuritySymbol("CMF")),
        SecurityDistributionKey(symbol=SecuritySymbol("CMF")),
    }


def test_a_record_cut_to_a_span_replays_only_that_span() -> None:
    """The cut is what makes a published-study reproduction answerable: without it the replay
    samples a different period than the study did, and a disagreement cannot be attributed."""

    history = _history(600).restricted_to(start=date(1980, 1, 1), end=date(1989, 12, 1))

    assert history.months[0] == date(1980, 1, 1)
    assert history.months[-1] == date(1989, 12, 1)
    assert len(history.months) == 120
    # The cut takes a slice of the ORIGINAL series, not a re-derivation of them: month 1980-01
    # is month 120 of a record starting 1970-01, so it must still carry month 120's rate.
    assert history.short_rate[0] == pytest.approx(0.01 + 120 * 0.0001)


def test_an_open_bound_cuts_only_the_end_it_names() -> None:
    history = _history(600).restricted_to(start=None, end=date(1979, 12, 1))

    assert history.months[0] == date(1970, 1, 1)
    assert history.months[-1] == date(1979, 12, 1)


def test_a_span_the_record_does_not_reach_is_rejected() -> None:
    """Silently returning an empty record would surface as "too few months for a window" from
    the sampler, one layer away from the span that actually caused it."""

    with pytest.raises(ValueError, match="falls in"):
        _history(600).restricted_to(start=date(2500, 1, 1), end=None)


def test_a_record_span_that_ends_before_it_starts_is_rejected() -> None:
    with pytest.raises(ValueError, match="ends before it starts"):
        HistoricalWindowsProviderConfig(record_start=date(1990, 1, 1), record_end=date(1980, 1, 1))


def test_out_of_order_months_are_rejected() -> None:
    """The month axis is what every date-addressed claim about the record rests on — a window's
    start, the span in provenance, the cut above. Unsorted, all three silently lie."""

    with pytest.raises(ValueError, match="strictly increasing"):
        MacroHistory(
            months=(date(1970, 2, 1), date(1970, 1, 1)),
            short_rate=np.zeros(2),
            term_spread=np.zeros(2),
            corporate_aaa_yield=np.zeros(2),
            corporate_baa_yield=np.zeros(2),
            equity_level=np.ones(2),
            cpi_level=np.ones(2),
        )


def test_the_provenance_names_the_span_the_rollouts_came_from() -> None:
    """A replay result is about a period. Reporting the window count without it leaves the
    reader unable to tell a 1926-1995 answer from a 1926-2026 one."""

    bundle = _model().materialize(window_starts=(date(1989, 12, 1), date(1970, 1, 1)), horizon_months=360)

    assert bundle.provenance["record_start"] == "1970-01-01"
    assert bundle.provenance["record_end"] == "2019-12-01"
    assert bundle.provenance["window_starts"] == ("1989-12-01", "1970-01-01")


def test_the_config_cuts_the_record_before_the_sampler_sees_it(tmp_path: Path) -> None:
    """The bound has to reach the realized model, not just be stored on the config."""

    _write_evidence(tmp_path)
    model = HistoricalWindowsProviderConfig(
        evidence_dir=tmp_path, record_start=date(1975, 1, 1), record_end=date(1984, 12, 1)
    ).realize_model()

    assert len(model.history.months) == 120


def test_an_instrument_prices_off_the_curve_it_names() -> None:
    """The point of naming a curve: a corporate sleeve earns what corporates earned, not a
    government yield plus a guessed constant. The three curves are separated in `_history`, so
    the payout recovers which one was actually read."""

    history = _history(400)
    horizon = 120
    payouts = {}
    for curve in (YieldCurve.GOVERNMENT, YieldCurve.CORPORATE_AAA, YieldCurve.CORPORATE_BAA):
        model = HistoricalWindowsModel(
            history=history,
            instruments=(BondFundSpec(symbol=BOND, maturity_years=6.0, initial_price_usd=100.0, yield_curve=curve),),
        )
        bundle = model.materialize(window_starts=(history.months[0],), horizon_months=horizon)
        payout = bundle.level_matrix(SecurityDistributionKey(symbol=BOND), rollout_count=1, horizon_months=horizon)
        # Month 1's coupon is struck on month 0's yield against the initial 100 of mark, so it
        # reads back that curve's month-0 level directly.
        payouts[curve] = float(payout[0, 1]) * 12.0 / 100.0

    # The government leg still interpolates: at 6 years the curve fraction is 6/10, so it is
    # the short rate plus 0.6 of the term spread. The corporate legs are read straight off.
    assert payouts[YieldCurve.GOVERNMENT] == pytest.approx(0.01 + 0.6 * 0.005)
    assert payouts[YieldCurve.CORPORATE_AAA] == pytest.approx(0.02)
    assert payouts[YieldCurve.CORPORATE_BAA] == pytest.approx(0.03)


def test_a_spread_still_adjusts_the_named_curve() -> None:
    """Municipals yield LESS than their curve pre-tax; the spread survived the rewrite."""

    horizon = 120
    model = HistoricalWindowsModel(
        history=_history(400),
        instruments=(
            BondFundSpec(
                symbol=BOND,
                maturity_years=6.0,
                initial_price_usd=100.0,
                yield_curve=YieldCurve.CORPORATE_AAA,
                spread=-0.004,
            ),
        ),
    )
    bundle = model.materialize(window_starts=(model.history.months[0],), horizon_months=horizon)
    payout = bundle.level_matrix(SecurityDistributionKey(symbol=BOND), rollout_count=1, horizon_months=horizon)

    assert float(payout[0, 1]) * 12.0 / 100.0 == pytest.approx(0.02 - 0.004)


def _ex_us_levels(start: date, end: date) -> pl.DataFrame:
    """A strictly increasing ex-US index, so each value names its month, as `_history` does."""

    months = [month for month in _month_seq(2400, 1900) if start <= month <= end]
    return pl.DataFrame({"month": months, "value": [50.0 + index for index in range(len(months))]})


def test_the_ex_us_record_keeps_every_series_month_for_month() -> None:
    """US and ex-US equity must come from the same months, so a replay window or bootstrap block
    carries one month's world. The ex-US span here starts inside the record and runs past its
    end, so the record is cut at the front only."""

    history = _history(600)
    ex_us = _ex_us_levels(date(1980, 1, 1), date(2030, 12, 1))
    joint = history.with_ex_us_equity(ex_us)

    assert joint.months == history.months[120:]
    np.testing.assert_array_equal(joint.short_rate, history.short_rate[120:])
    np.testing.assert_array_equal(joint.equity_level, history.equity_level[120:])
    assert joint.ex_us_equity_level is not None
    np.testing.assert_array_equal(joint.ex_us_equity_level, ex_us["value"].to_numpy()[: len(joint.months)])

    cut = joint.restricted_to(start=date(1990, 1, 1), end=None)
    assert cut.ex_us_equity_level is not None
    assert cut.ex_us_equity_level[0] == ex_us.filter(pl.col("month") == date(1990, 1, 1))["value"][0]


def test_a_hole_in_the_ex_us_series_splits_replayable_windows() -> None:
    """A missing ex-US month stays a boundary instead of making distant months neighbours."""

    ex_us = _ex_us_levels(date(1980, 1, 1), date(1990, 12, 1)).filter(pl.col("month") != date(1985, 6, 1))
    joint = _history(600).with_ex_us_equity(ex_us)
    replay = HistoricalWindowsModel(history=joint)

    assert date(1985, 6, 1) not in joint.months
    starts = replay.window_starts(12)
    assert date(1985, 5, 1) not in starts
    assert date(1985, 7, 1) in starts
    for start in starts:
        path = replay.market_paths(window_starts=(start,), horizon_months=12)
        index = joint.months.index(start)
        dates = joint.months[index : index + 13]
        assert len(dates) == 13
        assert all(loading.is_next_calendar_month(left, right) for left, right in pairwise(dates))
        assert path.equity_total_return_index is not None


def test_an_ex_us_series_outside_the_record_is_rejected() -> None:
    with pytest.raises(ValueError, match="no shared months"):
        _history(600).with_ex_us_equity(_ex_us_levels(date(2050, 1, 1), date(2051, 1, 1)))


def _write_ex_us_evidence(directory: Path, *, start_year: int, months: int, percent: float) -> None:
    title = "     Value-Weight Dollar Returns      All 4 Data Items Not Reqd\r\n          Mkt   High    Low\r\n"
    rows = "".join(f"{m.year}{m.month:02d}  {percent:5.2f}   0.00   0.00\r\n" for m in _month_seq(months, start_year))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Ind_all.Dat", f"\r\n{title}{rows}\r\n")
    (directory / sources.FRENCH_INTERNATIONAL_INDICES.output_filename).write_bytes(buffer.getvalue())


def test_ex_us_load_is_independent_and_can_be_composed_with_the_us_record(tmp_path: Path) -> None:
    """Loading ex-US returns separately leaves the US record intact until a caller composes them."""

    _write_evidence(tmp_path)
    _write_ex_us_evidence(tmp_path, start_year=1975, months=120, percent=2.0)

    us_only = load_macro_history(tmp_path)
    ex_us = load_ex_us_equity_levels(tmp_path)
    joint = us_only.with_ex_us_equity(ex_us)

    assert len(us_only.months) == 400
    assert us_only.ex_us_equity_level is None
    assert (joint.months[0], joint.months[-1]) == (date(1975, 1, 1), date(1984, 12, 1))
    assert joint.ex_us_equity_level is not None
    np.testing.assert_allclose(joint.ex_us_equity_level[1:] / joint.ex_us_equity_level[:-1], 1.02)
    np.testing.assert_allclose(joint.equity_level[1:] / joint.equity_level[:-1], 1.013)


if __name__ == "__main__":
    pytest_bazel.main()
