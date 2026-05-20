from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
from pydantic import ValidationError

from augur.fit.market_config import load_market_config, parse_market_config

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "market_config.example.json"
EXPECTED_TOP_LEVEL_KEYS = frozenset({"source_data", "location_market_sources"})
EXPECTED_SOURCE_DATA_KEYS = frozenset(
    {
        "fred_sp500_csv",
        "yahoo_spy_adjusted_json",
        "fred_cpi_us_csv",
        "fred_sf_rent_cpi_csv",
        "fred_sfxrsa_csv",
        "fred_fhfa_sf_oakland_berkeley_csv",
        "fred_mortgage30_csv",
        "zillow_city_zhvi_csv",
        "zillow_home_value_regions",
        "minimum_aligned_months",
    }
)
EXPECTED_LOCATION_MARKET_SOURCE_KEYS = frozenset({"home_value", "rent"})


def _config_payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CONFIG_PATH.read_text(encoding="utf-8")))


def test_example_market_config_is_the_public_file_boundary_contract() -> None:
    payload = _config_payload()
    config = parse_market_config(payload)

    assert set(payload) == EXPECTED_TOP_LEVEL_KEYS
    assert set(payload["source_data"]) == EXPECTED_SOURCE_DATA_KEYS
    assert set(payload["location_market_sources"]) == EXPECTED_LOCATION_MARKET_SOURCE_KEYS
    assert sorted(config.source_data.zillow_home_value_regions) == ["home", "vallejo_home"]
    assert config.location_market_sources.home_value["san_francisco_ca"] == "home"
    assert config.location_market_sources.rent["mare_island_vallejo_ca"] == "rent"


def test_load_market_config_rejects_stale_runtime_knobs(tmp_path: Path) -> None:
    payload = _config_payload()
    payload.update(
        {
            "seed": 123,
            "rollout_count": 50,
            "horizon_months": 360,
            "runtime_sampler_label": "stale",
            "sampler": "stale",
            "market_provider_label": "stale",
        }
    )
    config_path = tmp_path / "market_config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match=r"seed|rollout_count|horizon_months|runtime_sampler_label"):
        load_market_config(config_path)


def test_home_value_location_sources_must_reference_configured_factors() -> None:
    payload = _config_payload()
    payload["location_market_sources"]["home_value"]["unknown_ca"] = "missing_home_factor"

    with pytest.raises(ValidationError, match=r"location_market_sources\.home_value"):
        parse_market_config(payload)


def test_rent_location_sources_must_reference_configured_factors() -> None:
    payload = _config_payload()
    payload["location_market_sources"]["rent"]["unknown_ca"] = "missing_rent_factor"

    with pytest.raises(ValidationError, match=r"location_market_sources\.rent"):
        parse_market_config(payload)


if __name__ == "__main__":
    pytest_bazel.main()
