from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel

from augur.fit import evidence_data
from augur.fit.data import load_evidence, load_fred_only_evidence


def test_configured_evidence_source_errors_raise_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    malformed_yahoo = tmp_path / "malformed_yahoo_spy_chart_adjusted.json"
    malformed_yahoo.write_text("{not json", encoding="utf-8")
    real_resolver = evidence_data._source_path

    # Redirect only the SPY adjusted-close source to the malformed file (other
    # sources resolve normally); the loader must surface the JSON parse error
    # rather than swallow it.
    def fake_resolver(repo_relative: str) -> Path:
        return (
            malformed_yahoo if repo_relative == evidence_data.YAHOO_SPY_ADJUSTED_JSON else real_resolver(repo_relative)
        )

    monkeypatch.setattr(evidence_data, "_source_path", fake_resolver, raising=True)

    with pytest.raises(json.JSONDecodeError):
        load_evidence()


def test_explicit_fred_only_evidence_is_synthesized_and_labeled() -> None:
    historical, evidence = load_fred_only_evidence()

    # `historical` carries typed LevelSeriesKeys; the evidence layer keeps the wire-id strings.
    assert tuple(factor.wire_id for factor in historical.factor_names) == evidence.factor_names
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
