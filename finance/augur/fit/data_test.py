from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.fit import evidence_data
from finance.augur.fit.data import load_evidence, load_fred_only_evidence
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

    # `historical` carries typed LevelSeriesKeys; the evidence layer keeps the wire-id strings.
    assert tuple(factor.wire_id for factor in historical.series_names) == evidence.series_names
    assert evidence.monthly_log_returns.shape[0] == len(evidence.monthly_return_months)
    assert evidence.latest_observations["evidence_mode"] == {
        "mode": "fred_only_synthesized",
        "explicit": True,
        "description": "FRED-only synthesized evidence explicitly selected; Yahoo SPY and Zillow ZHVI were not loaded.",
    }
    assert "spy_adjusted_close_latest" not in evidence.latest_observations
    assert "zillow_home_value_latest_by_factor" not in evidence.latest_observations
    assert "case_shiller_home_value_latest_by_factor" in evidence.latest_observations


if __name__ == "__main__":
    pytest_bazel.main()
