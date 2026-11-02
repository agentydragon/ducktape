from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.fit import evidence_data
from finance.augur.fit.data import aligned_as_of_observations, load_evidence, load_evidence_segments
from finance.augur.model.conditioning import ObservationUnits
from finance.augur.model.series import SP500_KEY, HomeValueKey, RentKey
from finance.evidence import sources


def test_configured_evidence_source_errors_raise_by_default(
    monkeypatch: pytest.MonkeyPatch, synthetic_evidence_dir: Path
) -> None:
    real_source_bytes = evidence_data._source_bytes

    # Redirect only the SPY adjusted-close source to malformed bytes (other sources
    # resolve normally); the loader must surface the JSON parse error, not swallow it.
    def fake_source_bytes(source: sources.EvidenceSource) -> bytes:
        if source is sources.YAHOO_SPY:
            return b"{not json"
        return real_source_bytes(source)

    monkeypatch.setattr(evidence_data, "_source_bytes", fake_source_bytes, raising=True)

    with pytest.raises(json.JSONDecodeError):
        load_evidence()


def test_explicit_fred_only_evidence_is_synthesized_and_labeled(synthetic_evidence_dir: Path) -> None:
    historical, evidence = load_evidence(fred_only=True)

    # Both sides carry the SAME typed keys — no wire-id round trip between them. This used to
    # assert `tuple(f.wire_id for f in historical.series_names) == evidence.series_names`, which
    # is exactly the flatten-and-reparse the typing removed.
    assert historical.series_names == evidence.series_names
    assert evidence.monthly_log_returns.shape[0] == len(evidence.monthly_return_months)
    assert evidence.metadata.mode is not None
    assert evidence.metadata.mode.mode == "fred_only_synthesized"
    assert evidence.metadata.mode.explicit
    assert evidence.metadata.mode.description == (
        "FRED-only synthesized evidence explicitly selected; Yahoo SPY and Zillow ZHVI were not loaded."
    )
    assert set(evidence.latest_observations) == set(evidence.series_names)
    assert evidence.latest_observations[SP500_KEY].source_id == f"public:{sources.FRED_SP500.provenance_label}"
    assert all(point.units == ObservationUnits.INDEX_POINTS for point in evidence.latest_observations.values())
    for factor, point in evidence.latest_observations.items():
        if isinstance(factor, HomeValueKey):
            assert point.source_id == f"public:{sources.FRED_SFXRSA.provenance_label}"


def test_fred_only_dated_levels_preserve_gap_without_fabricating_a_return(synthetic_evidence_dir: Path) -> None:
    cpi_path = synthetic_evidence_dir / sources.FRED_CPI.output_filename
    lines = cpi_path.read_text().splitlines()
    cpi_path.write_text("\n".join(line for line in lines if not line.startswith("2025-10-")) + "\n")

    history, evidence = load_evidence_segments(fred_only=True)

    assert history.gaps[0].before_month == "2025-09"
    assert history.gaps[0].after_month == "2025-11"
    assert history.gaps[0].missing_months == ("2025-10",)
    assert all(len(segment.months) == segment.end_index - segment.start_index + 1 for segment in history.segments)
    assert "2025-10" not in evidence.monthly_return_months
    assert "2025-11" not in evidence.monthly_return_months
    assert "2025-12" in evidence.monthly_return_months


def test_full_evidence_history_keeps_each_return_on_its_calendar_month(synthetic_evidence_dir: Path) -> None:
    historical, evidence = load_evidence()

    assert historical.months[1:] == evidence.monthly_return_months
    np.testing.assert_allclose(np.diff(np.log(historical.levels), axis=0), evidence.monthly_log_returns)


@pytest.mark.parametrize("fred_only", [False, True])
def test_missing_cpi_month_is_rejected_by_both_loaders(synthetic_evidence_dir: Path, fred_only: bool) -> None:
    cpi_path = synthetic_evidence_dir / sources.FRED_CPI.output_filename
    lines = cpi_path.read_text().splitlines()
    cpi_path.write_text("\n".join(line for line in lines if not line.startswith("2025-10-")) + "\n")

    with pytest.raises(ValueError, match="calendar gaps"):
        load_evidence(fred_only=fred_only)

    segmented, evidence = load_evidence_segments(fred_only=fred_only)
    assert segmented.gaps[0].missing_months == ("2025-10",)
    assert segmented.transition_pairs
    assert len(segmented.transition_pairs) == len(evidence.monthly_return_months)
    np.testing.assert_array_equal(segmented.levels, evidence.monthly_level_values)
    assert segmented.months == evidence.monthly_level_months
    expected_returns = np.asarray(
        [np.log(segmented.levels[end]) - np.log(segmented.levels[start]) for start, end in segmented.transition_pairs]
    )
    np.testing.assert_allclose(expected_returns, evidence.monthly_log_returns)


def test_fred_only_preserves_a_negative_latest_mortgage_rate(synthetic_evidence_dir: Path) -> None:
    mortgage_path = synthetic_evidence_dir / sources.FRED_MORTGAGE30.output_filename
    rows = mortgage_path.read_text().splitlines()
    rows[-1] = f"{rows[-1].split(',')[0]},-0.25"
    mortgage_path.write_text("\n".join(rows) + "\n")

    _, evidence = load_evidence(fred_only=True)

    assert evidence.current_mortgage30_rate_pct == -0.25
    assert evidence.metadata.auxiliary_observations["mortgage30"].value == -0.25


@pytest.mark.parametrize("fred_only", [False, True])
@pytest.mark.parametrize("invalid_index", ["0", "-1"])
def test_fred_price_indices_must_be_positive_before_log_returns(
    synthetic_evidence_dir: Path, fred_only: bool, invalid_index: str
) -> None:
    cpi_path = synthetic_evidence_dir / sources.FRED_CPI.output_filename
    rows = cpi_path.read_text().splitlines()
    rows[-1] = f"{rows[-1].split(',')[0]},{invalid_index}"
    cpi_path.write_text("\n".join(rows) + "\n")

    with pytest.raises(ValueError, match="finite positive levels"):
        load_evidence(fred_only=fred_only)


def test_full_evidence_anchors_preserve_actual_source_units(synthetic_evidence_dir: Path) -> None:
    _, evidence = load_evidence()
    assert set(evidence.latest_observations) == set(evidence.series_names)
    assert evidence.latest_observations[SP500_KEY].source_id == f"public:{sources.YAHOO_SPY.provenance_label}"
    assert evidence.latest_observations[SP500_KEY].units == ObservationUnits.USD_PER_UNIT
    for factor, point in evidence.latest_observations.items():
        if isinstance(factor, HomeValueKey):
            assert point.units == ObservationUnits.USD
            assert point.source_id == f"public:{sources.ZILLOW_ZHVI.provenance_label}"
        if isinstance(factor, RentKey):
            assert point.units == ObservationUnits.USD_PER_MONTH
            assert point.source_id == f"public:{sources.ZILLOW_ZORI.provenance_label}"
    assert evidence.metadata.auxiliary_observations["sp500_price"].units == ObservationUnits.INDEX_POINTS
    assert evidence.metadata.auxiliary_observations["mortgage30"].units == ObservationUnits.PERCENT
    assert evidence.metadata.return_sources


def test_vecm_anchors_use_one_common_month_and_spy_adjusted_levels(synthetic_evidence_dir: Path) -> None:
    history, evidence = load_evidence_segments()
    anchors = aligned_as_of_observations(evidence)
    as_of = history.months[-1]
    sp500_index = evidence.series_names.index(SP500_KEY)

    assert {point.observed_at.isoformat() for point in anchors.values()} == {f"{as_of}-01"}
    assert anchors[SP500_KEY].source_id == f"public:{sources.YAHOO_SPY.provenance_label}"
    assert anchors[SP500_KEY].units == ObservationUnits.USD_PER_UNIT
    assert anchors[SP500_KEY].value == pytest.approx(evidence.monthly_level_values[-1, sp500_index])
    assert (
        evidence.metadata.auxiliary_observations["sp500_price"].source_id
        == f"public:{sources.FRED_SP500.provenance_label}"
    )


def test_full_evidence_reads_each_zillow_table_once_for_all_cities(
    monkeypatch: pytest.MonkeyPatch, synthetic_evidence_dir: Path
) -> None:
    original = evidence_data._source_bytes
    reads: list[sources.EvidenceSource] = []

    def track(source: sources.EvidenceSource) -> bytes:
        reads.append(source)
        return original(source)

    monkeypatch.setattr(evidence_data, "_source_bytes", track)
    _, evidence = load_evidence()
    assert reads.count(sources.ZILLOW_ZHVI) == 1
    assert reads.count(sources.ZILLOW_ZORI) == 1
    home = [point.value for key, point in evidence.latest_observations.items() if isinstance(key, HomeValueKey)]
    assert len(home) == 2
    assert home[0] != home[1]


if __name__ == "__main__":
    pytest_bazel.main()
