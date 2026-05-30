"""TestClient coverage for the exogenous-only calibration surface.

Builds the app from the public fixture config (which configures the example OpenAI
catalog scored against the `openai_pe` preset), injecting a `mock_manifold_client` so the
run stays hermetic (no network). `/api/bootstrap` surfaces the catalog info;
`/api/calibration/run` does a small run with the injected live prices.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from augur.api.config import load_augur_config
from augur.api.server import create_app_from_augur_config
from augur.calibration.catalog import MarketCatalog
from augur.calibration.testing import mock_manifold_client
from util.bazel.runfiles import get_required_path


@pytest.fixture
def client() -> Iterator[TestClient]:
    config = load_augur_config(get_required_path("_main/augur/api/testdata/config.yaml"))
    assert config.calibration_catalog is not None
    catalog = MarketCatalog.from_yaml(config.calibration_catalog.catalog_path)
    # Every market resolves to the same fixed YES probability so the run is hermetic.
    prices = {market.manifold_id: 0.5 for market in catalog.markets}
    app = create_app_from_augur_config(config, price_client=mock_manifold_client(prices))
    with TestClient(app) as test_client:
        yield test_client


def test_bootstrap_surfaces_calibration_catalog(client: TestClient) -> None:
    response = client.get("/api/bootstrap")
    assert response.status_code == 200, response.text
    calibration = response.json()["calibration"]
    assert calibration["issuer"] == "openai"
    assert calibration["label"] == "OpenAI (example Manifold catalog)"


def test_run_calibration(client: TestClient) -> None:
    response = client.post(
        "/api/calibration/run", json={"preset_id": "openai_pe", "horizon_months": 24, "rollouts": 16, "seed": 1701}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset_id"] == "openai_pe"

    result = body["result"]
    assert result["issuer"] == "openai"
    assert result["horizon_months"] == 24
    assert result["rollout_count"] == 16
    # The example catalog ships both exact (scored) and surfaced markets.
    assert isinstance(result["clean"], list)
    assert result["clean"]
    assert isinstance(result["surfaced"], list)
    assert result["surfaced"]
    clean_row = result["clean"][0]
    # Every market's p_market is the injected live stub price.
    assert clean_row["p_market"] == 0.5
    # Snake_case on the wire (the frontend camelizes). p_model/kl_bits/resolution_deadline
    # are optional (dropped when None — e.g. no rollout resolved within the horizon), so we
    # only require the always-present fields here.
    assert {"slug", "mapping_kind", "p_market", "ci95", "n_resolved", "unresolved"} <= set(clean_row)
    surfaced_row = result["surfaced"][0]
    assert {"slug", "question", "url", "mappability", "p_market"} <= set(surfaced_row)
    assert surfaced_row["p_market"] == 0.5
    assert surfaced_row["url"].startswith("https://manifold.markets/")

    fan = body["mark_fan"]
    assert fan["issuer"] == "openai"
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
    # Omitting `preset_id` resolves to the deployment's `default_exogenous_preset_id`
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


if __name__ == "__main__":
    pytest_bazel.main()
