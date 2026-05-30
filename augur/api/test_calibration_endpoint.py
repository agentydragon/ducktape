"""TestClient coverage for the exogenous-only calibration surface.

Builds the app from the public fixture config (which configures the example OpenAI
catalog scored against the `openai_pe` preset). `/api/bootstrap` surfaces the catalog
info; `/api/calibration/run` does a small, offline (`live=false`) run. No network.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from augur.api.config import load_augur_config
from augur.api.server import create_app_from_augur_config
from util.bazel.runfiles import get_required_path


@pytest.fixture
def client() -> Iterator[TestClient]:
    config = load_augur_config(get_required_path("_main/augur/api/testdata/config.yaml"))
    with TestClient(create_app_from_augur_config(config)) as test_client:
        yield test_client


def test_bootstrap_surfaces_calibration_catalog(client: TestClient) -> None:
    response = client.get("/api/bootstrap")
    assert response.status_code == 200, response.text
    calibration = response.json()["calibration"]
    assert calibration["issuer"] == "openai"
    assert calibration["default_preset_id"] == "openai_pe"
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
    assert result["price_source"] == "curation-snapshot"
    # The example catalog ships both exact (scored) and surfaced markets.
    assert isinstance(result["clean"], list)
    assert result["clean"]
    assert isinstance(result["surfaced"], list)
    assert result["surfaced"]
    clean_row = result["clean"][0]
    # Snake_case on the wire (the frontend camelizes). p_model/abs_gap/resolution_deadline
    # are optional (dropped when None — e.g. no rollout resolved within the horizon), so we
    # only require the always-present fields here.
    assert {"slug", "mapping_kind", "p_market", "ci95", "n_resolved", "unresolved"} <= set(clean_row)
    surfaced_row = result["surfaced"][0]
    assert {"slug", "question", "mappability", "p_market"} <= set(surfaced_row)

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


def test_run_unknown_preset_is_400(client: TestClient) -> None:
    response = client.post("/api/calibration/run", json={"preset_id": "nope"})
    assert response.status_code == 400, response.text


def test_unknown_calibration_route_still_404(client: TestClient) -> None:
    # The catch-all must remain reachable past the new routes.
    response = client.get("/api/calibration/does-not-exist")
    assert response.status_code == 404, response.text


if __name__ == "__main__":
    pytest_bazel.main()
