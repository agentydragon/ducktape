"""TestClient coverage for the model-only calibration surface.

Builds the app from the public fixture config (which configures the example OpenAI
catalog scored against the `openai_pe` preset), injecting multi-platform mock clients
so the run stays hermetic (no network). `/api/calibration` surfaces the catalog info;
`/api/calibration/run` does a small run with the injected live prices.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from fastapi.testclient import TestClient

from augur.api.config import Config
from augur.api.server import create_app_from_augur_config, static_price_clients
from augur.calibration.catalog import MarketCatalog
from augur.calibration.platform import Platform
from augur.calibration.testing import mock_price_clients
from augur.model.sample_sanity import LevelSeriesSanityCheck, PrivateEquityMarkSanityCheck, SampleSanitySpec
from augur.model.series import IssuerId, SP500Key


def _client_for(config: Config) -> TestClient:
    catalog = MarketCatalog.from_yaml(config.calibration_catalog.catalog_path)
    # Every market (and every bucket of every categorical family) resolves to the same fixed YES
    # probability so the run is hermetic.
    by_platform: defaultdict[Platform, dict[str, float]] = defaultdict(dict)
    for market in catalog.markets:
        by_platform[market.platform][market.market_id] = 0.5
    for bucket_family in catalog.bucket_families:
        for bucket_member in bucket_family.buckets:
            by_platform[bucket_family.platform][bucket_member.market_id] = 0.5
    for ladder_family in catalog.threshold_ladder_families:
        for ladder_member in ladder_family.thresholds:
            by_platform[ladder_family.platform][ladder_member.market_id] = 0.5
    for date_family in catalog.date_ladder_families:
        for date_member in date_family.dates:
            by_platform[date_family.platform][date_member.market_id] = 0.5
    return TestClient(
        create_app_from_augur_config(config, price_clients=static_price_clients(mock_price_clients(dict(by_platform))))
    )


@pytest.fixture
def client(augur_config: Config) -> Iterator[TestClient]:
    with _client_for(augur_config) as test_client:
        yield test_client


def test_calibration_info_endpoint(client: TestClient) -> None:
    response = client.get("/api/calibration")
    assert response.status_code == 200, response.text
    calibration = response.json()
    assert calibration["issuers"] == ["openai"]
    assert calibration["label"] == "OpenAI (example Manifold catalog)"


def test_run_calibration(client: TestClient) -> None:
    response = client.post(
        "/api/calibration/run", json={"preset_id": "openai_pe", "horizon_months": 24, "rollouts": 16, "seed": 1701}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset_id"] == "openai_pe"

    result = body["result"]
    assert result["horizon_months"] == 24
    assert result["rollout_count"] == 16
    # PE markets now carry their issuer in `channel`; the example catalog scores `openai`.
    assert any(row.get("channel") == "openai" for row in result["clean"])
    # The example catalog ships both exact (scored) and surfaced markets.
    assert isinstance(result["clean"], list)
    assert result["clean"]
    assert isinstance(result["surfaced"], list)
    assert result["surfaced"]
    clean_row = result["clean"][0]
    # Every market's p_market is the injected live stub price.
    assert clean_row["p_market"] == 0.5
    # Snake_case on the wire (the frontend camelizes). p_model/kl_bits are optional (dropped when
    # None — e.g. no rollout resolved within the horizon), so we only require the always-present
    # fields here.
    assert {"market_id", "question", "url", "platform", "p_market", "ci95", "n_resolved", "unresolved"} <= set(
        clean_row
    )
    surfaced_row = result["surfaced"][0]
    assert {"market_id", "question", "url", "platform", "mappability", "p_market"} <= set(surfaced_row)
    assert surfaced_row["p_market"] == 0.5
    assert surfaced_row["url"].startswith("https://")

    # Macro markets score against the model's level channels: the S&P and Bitcoin bucket families
    # (multinomial) plus the crypto:btc point-in-time / ever-by-date markets in the clean table.
    assert {fam["channel"] for fam in result["categorical"]} == {"sp500", "crypto:btc"}
    family = next(fam for fam in result["categorical"] if fam["channel"] == "sp500")
    assert {"family_id", "question", "platform", "channel", "at_date", "buckets"} <= set(family)
    assert len(family["buckets"]) == 27
    assert any(row.get("channel") == "crypto:btc" for row in result["clean"])

    # One mark fan per scored issuer; the example catalog scores `openai`.
    assert [fan["issuer"] for fan in body["mark_fans"]] == ["openai"]
    fan = body["mark_fans"][0]
    assert fan["channel"] == "mark_usd_per_unit"
    assert fan["percentiles"] == [5.0, 25.0, 50.0, 75.0, 95.0]
    # One band per month, months 0..horizon inclusive.
    assert len(fan["months"]) == 25
    month0 = fan["months"][0]
    assert month0["month_index"] == 0
    # Month 0 sits at the issuer's current mark across every percentile (no dispersion yet).
    assert set(month0["values"]) == {"5.0", "25.0", "50.0", "75.0", "95.0"}
    assert all(value == 100.0 for value in month0["values"].values())


def test_run_calibration_defaults_to_shared_preset(client: TestClient) -> None:
    # Omitting `preset_id` resolves to the deployment's `default_model_id`
    # (the fixture pins `openai_pe`), and the response echoes the resolved preset.
    response = client.post("/api/calibration/run", json={"horizon_months": 24, "rollouts": 16, "seed": 1701})
    assert response.status_code == 200, response.text
    assert response.json()["preset_id"] == "openai_pe"


def test_run_unknown_preset_is_400(client: TestClient) -> None:
    response = client.post("/api/calibration/run", json={"preset_id": "nope"})
    assert response.status_code == 400, response.text


def test_unknown_calibration_route_still_404(client: TestClient) -> None:
    # Unknown API routes now get FastAPI's default 404 (the custom `/api/{full_path}`
    # catch-all is gone; nginx serves the SPA, so the app is API-only with no static fallback).
    response = client.get("/api/calibration/does-not-exist")
    assert response.status_code == 404, response.text


def test_run_calibration_without_sample_sanity_returns_empty_bands(client: TestClient) -> None:
    # The public fixture configures a `calibration_catalog` but no `sample_sanity_path`, so the
    # reasonableness-band feature is absent: the endpoint succeeds and returns an empty list.
    response = client.post("/api/calibration/run", json={"horizon_months": 24, "rollouts": 16, "seed": 1701})
    assert response.status_code == 200, response.text
    assert response.json()["sanity_bands"] == []


def _config_with_sample_sanity(augur_config: Config, tmp_path: Path) -> Config:
    """Fixture config whose `calibration_catalog.sample_sanity_path` points at a temp spec YAML.

    The spec reuses the live `openai_pe` model: one level-series band on `sp500` (the macro block
    anchors it at 1.0) and one PE-mark band on the catalog issuer `openai` (anchored at its current
    mark 100.0).
    """
    spec = SampleSanitySpec(
        horizon_months=24,
        rollout_count=16,
        level_checks=(LevelSeriesSanityCheck(key=SP500Key(), initial_value=1.0),),
        private_equity_mark_checks=(PrivateEquityMarkSanityCheck(issuer_id=IssuerId("openai"), initial_value=100.0),),
    )
    spec_path = tmp_path / "sample_sanity.yaml"
    spec_path.write_text(yaml.safe_dump(spec.model_dump(mode="json")), encoding="utf-8")

    catalog = augur_config.calibration_catalog.model_copy(update={"sample_sanity_path": spec_path})
    return augur_config.model_copy(update={"calibration_catalog": catalog})


def test_run_calibration_includes_sample_sanity_bands(tmp_path: Path, augur_config: Config) -> None:
    with _client_for(_config_with_sample_sanity(augur_config, tmp_path)) as client:
        response = client.post("/api/calibration/run", json={"horizon_months": 24, "rollouts": 16, "seed": 1701})
    assert response.status_code == 200, response.text
    bands = response.json()["sanity_bands"]
    assert bands

    # Both the level-series band (sp500) and the PE-mark band (openai) are evaluated.
    series_ids = {band["series_id"] for band in bands}
    assert "sp500" in series_ids
    assert any("openai" in series_id and "mark" in series_id for series_id in series_ids)

    # The deterministic anchor bands (sp500 -> 1.0, openai mark -> 100.0) pass; nothing fails.
    anchors = {band["series_id"]: band for band in bands if band["kind"] == "anchor"}
    assert anchors["sp500"]["status"] == "pass"
    assert anchors["sp500"]["month"] == 0
    openai_anchor = next(band for series_id, band in anchors.items() if "openai" in series_id)
    assert openai_anchor["status"] == "pass"
    assert openai_anchor["expected_lower"] == 100.0
    assert all(band["status"] != "fail" for band in bands)


if __name__ == "__main__":
    pytest_bazel.main()
