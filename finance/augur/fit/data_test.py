from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.fit import evidence_data
from finance.augur.fit.data import load_evidence, load_fred_only_evidence
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
    historical, evidence = load_fred_only_evidence()

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


if __name__ == "__main__":
    pytest_bazel.main()
