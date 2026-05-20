from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel

from augur.fit.data import load_evidence, load_fred_only_evidence
from augur.fit.market_config import load_market_config

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "market_config.example.json"


def _config_with_absolute_source_paths() -> dict[str, Any]:
    config = cast(dict[str, Any], json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    source = dict(config["source_data"])
    for key, value in source.items():
        if isinstance(value, str):
            source[key] = str((CONFIG_PATH.parent / value).resolve())
    config["source_data"] = source
    return config


def test_configured_market_source_errors_raise_by_default(tmp_path: Path) -> None:
    config = _config_with_absolute_source_paths()
    malformed_yahoo = tmp_path / "malformed_yahoo_spy_chart_adjusted.json"
    malformed_yahoo.write_text("{not json", encoding="utf-8")
    config["source_data"]["yahoo_spy_adjusted_json"] = str(malformed_yahoo)

    config_path = tmp_path / "market_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        load_evidence(load_market_config(config_path), config_path.parent)


def test_explicit_fred_only_evidence_is_synthesized_and_labeled() -> None:
    historical, evidence = load_fred_only_evidence(load_market_config(CONFIG_PATH), CONFIG_PATH.parent)

    assert historical.factor_names == evidence.factor_names
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
