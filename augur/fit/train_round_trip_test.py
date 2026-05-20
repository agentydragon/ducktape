"""Round-trip test: train the active VECM model offline, write the provider config +
blob, re-load via Pydantic + `<Model>MarketProviderConfig.realize_model(...)`,
and sample.

This is the public contract the augur server consumes at startup: read
`AugurConfig.market_provider`, dispatch via the discriminated union, and
sample without re-fitting from source CSVs."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml
from pydantic import TypeAdapter

from augur.fit.main import main as train_main
from augur.model.market_api import MarketSamplingRequest
from augur.model.market_provider_config import MarketProviderConfig, SimpleMarketProviderConfig
from augur.model.series import home_value_series_id, rent_series_id
from util.bazel.runfiles import get_required_path

_ADAPTER: TypeAdapter[MarketProviderConfig] = TypeAdapter(MarketProviderConfig)


def _write_market_config(path: Path) -> None:
    source_root = "_main/augur/data/market/source"

    def _source(name: str) -> str:
        return str(get_required_path(f"{source_root}/{name}"))

    path.write_text(
        yaml.safe_dump(
            {
                "source_data": {
                    "fred_sp500_csv": _source("fred_sp500.csv"),
                    "yahoo_spy_adjusted_json": _source("yahoo_spy_chart_adjusted.json"),
                    "fred_cpi_us_csv": _source("fred_cpi_us.csv"),
                    "fred_sf_rent_cpi_csv": _source("fred_sf_rent_cpi.csv"),
                    "fred_sfxrsa_csv": _source("fred_sfxrsa.csv"),
                    "fred_fhfa_sf_oakland_berkeley_csv": _source("fred_fhfa_sf_oakland_berkeley.csv"),
                    "fred_mortgage30_csv": _source("fred_mortgage30.csv"),
                    "zillow_city_zhvi_csv": _source("zillow_city_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv"),
                    "zillow_home_value_regions": {
                        "home": {"region_name": "San Francisco"},
                        "vallejo_home": {"region_name": "Vallejo"},
                    },
                },
                "location_market_sources": {
                    "home_value": {"san_francisco_ca": "home", "vallejo_ca": "vallejo_home"},
                    "rent": {"san_francisco_ca": "rent", "vallejo_ca": "rent"},
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("model_label", ["vecm"])
def test_train_then_load_and_sample(model_label: str, tmp_path: Path) -> None:
    out_manifest = tmp_path / "market_provider.yaml"
    out_blob = tmp_path / f"trained_{model_label}.npz"
    market_config = tmp_path / "market_config.yaml"
    _write_market_config(market_config)

    train_main(
        [
            "--market-config",
            str(market_config),
            "--model",
            model_label,
            "--out-provider-config",
            str(out_manifest),
            "--out-blob",
            str(out_blob),
        ]
    )

    assert out_manifest.exists()
    assert out_blob.exists()

    parsed = _ADAPTER.validate_python(yaml.safe_load(out_manifest.read_text(encoding="utf-8")))
    # Trainer only emits the active trained provider config; narrow away fixture
    # providers so the trained-provider fields are accessible below.
    assert not isinstance(parsed, SimpleMarketProviderConfig)
    assert parsed.type == model_label
    assert parsed.trained_blob == out_blob
    assert parsed.latest_observations  # non-empty; exact keys depend on the source-data schema

    model = parsed.realize_model(current_private_equity_price_usd=100.0)
    locations = sorted(parsed.location_market_sources.home_value)
    required_level_series = frozenset(
        {
            series_id
            for location in locations
            for series_id in (home_value_series_id(location), rent_series_id(location))
        }
    )
    sampled = model.sample(
        MarketSamplingRequest(rollout_seeds=(7, 8), horizon_months=12, required_level_series=required_level_series)
    )

    assert str(sampled.metadata["market_model_version_id"]).startswith("model_version:")
    assert sampled.metadata["current_private_equity_price_usd"] == 100.0
    assert {
        row["series_id"] for row in sampled.levels.select("series_id").unique().iter_rows(named=True)
    } == required_level_series
    for location in locations:
        assert sampled.level_matrix(home_value_series_id(location), rollout_count=2, horizon_months=12).shape == (2, 13)
        assert sampled.level_matrix(rent_series_id(location), rollout_count=2, horizon_months=12).shape == (2, 13)


if __name__ == "__main__":
    pytest_bazel.main()
