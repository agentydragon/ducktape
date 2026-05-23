from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from augur.api.http_app import create_augur_backend_app
from augur.api.scenario_set import ScenarioSet
from augur.product.portfolio import ProductPortfolioResponse
from augur.product.projection import MetricFanRequest, RolloutRequest

type ProductPortfolioTestHandler = Callable[[], ProductPortfolioResponse]


def _scenario_set_body() -> dict:
    return {
        "scenario_set_id": "route_test",
        "title": "Route test",
        "sampling_request": {"rollout_count": 1, "horizon_months": 1, "seed": 1},
        "scenarios": [
            {
                "scenario_id": "sf_house",
                "label": "SF house",
                "actors": [{"actor_id": "owner", "label": "Owner", "role": "primary_owner"}],
            }
        ],
    }


@pytest.fixture
def empty_product_portfolio() -> ProductPortfolioTestHandler:
    def handler() -> ProductPortfolioResponse:
        return ProductPortfolioResponse(
            as_of_date="2026-05-14",
            cash_usd=0.0,
            public_securities=(),
            total_public_security_value_usd=0.0,
            total_public_security_cost_basis_usd=0.0,
        )

    return handler


@pytest.fixture
def sample_product_portfolio() -> ProductPortfolioTestHandler:
    def handler() -> ProductPortfolioResponse:
        return ProductPortfolioResponse.model_validate(
            {
                "as_of_date": "2026-05-14",
                "cash_usd": 50_000.0,
                "public_securities": [
                    {
                        "position_id": "sp500_proxy",
                        "account_id": "taxable_brokerage",
                        "account_label": "Taxable Brokerage",
                        "label": "SP500 Proxy",
                        "symbol": "VOO",
                        "security_kind": "etf",
                        "value_series_id": "sp500",
                        "unit_value_usd": 500.0,
                        "quantity": 300.0,
                        "current_value_usd": 150_000.0,
                        "total_cost_basis_usd": 110_000.0,
                        "lots": [],
                    }
                ],
                "total_public_security_value_usd": 150_000.0,
                "total_public_security_cost_basis_usd": 110_000.0,
            }
        )

    return handler


def _metric_fan(request: MetricFanRequest) -> dict:
    return {
        "exogenous_model_id": request.scenario.exogenous_model_id,
        "metric": request.metric,
        "monthly_metric_fan": {"row_count": 1, "columns": {"month_index": [0], "percentile": [50.0], "value": [1.0]}},
        "terminal_metric_percentiles": {"row_count": 1, "columns": {"percentile": [50.0], "value": [1.0]}},
        "failed_count": 0,
    }


def _rollout(request: RolloutRequest) -> dict:
    return {
        "exogenous_model_id": request.scenario.exogenous_model_id,
        "rollout": {
            "seed": int(request.seed),
            "failed": False,
            "monthly_metrics": {"row_count": 1, "columns": {"month_index": [0], "cash_usd": [50000.0]}},
            "terminal_metrics": {
                "cash_usd": 50000.0,
                "public_security_value_usd": 0.0,
                "liquid_net_worth_usd": 50000.0,
                "net_worth_usd": 50000.0,
                "shortfall_usd": 0.0,
            },
        },
    }


def test_scenario_set_route_is_registered_and_invokes_handler(
    empty_product_portfolio: ProductPortfolioTestHandler,
) -> None:
    seen_scenario_set: ScenarioSet | None = None

    def scenario_set_run(scenario_set: ScenarioSet) -> dict:
        nonlocal seen_scenario_set
        seen_scenario_set = scenario_set
        return {
            "scenario_set_id": scenario_set.scenario_set_id,
            "scenario_results": [{"scenario_id": scenario_set.scenarios[0].scenario_id}],
        }

    app = create_augur_backend_app(
        title="test",
        bootstrap=lambda: {"ok": True},
        product_portfolio=empty_product_portfolio,
        product_metric_fan=_metric_fan,
        product_rollout=_rollout,
        scenario_set_run=scenario_set_run,
    )
    assert not any(getattr(route, "path", None) == "/api/projection/run" for route in app.routes)
    response = TestClient(app).post("/api/scenario_sets/run", json=_scenario_set_body())

    assert response.status_code == 200
    assert seen_scenario_set is not None
    assert seen_scenario_set.scenario_set_id == "route_test"
    assert seen_scenario_set.scenarios[0].scenario_id == "sf_house"
    payload = response.json()
    assert payload["scenario_set_id"] == "route_test"
    assert payload["scenario_results"][0]["scenario_id"] == "sf_house"


def test_product_portfolio_route_is_registered_and_invokes_handler(
    sample_product_portfolio: ProductPortfolioTestHandler,
) -> None:
    app = create_augur_backend_app(
        title="test",
        bootstrap=lambda: {"ok": True},
        product_portfolio=sample_product_portfolio,
        product_metric_fan=_metric_fan,
        product_rollout=_rollout,
    )

    response = TestClient(app).get("/api/product/portfolio")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cash_usd"] == 50_000.0
    assert payload["public_securities"][0]["symbol"] == "VOO"


def test_scenario_set_route_validates_request_with_pydantic(
    empty_product_portfolio: ProductPortfolioTestHandler,
) -> None:
    called = False

    def scenario_set_run(scenario_set: ScenarioSet) -> dict:
        nonlocal called
        called = True
        return {"scenario_set_id": scenario_set.scenario_set_id, "scenario_results": []}

    app = create_augur_backend_app(
        title="test",
        bootstrap=lambda: {"ok": True},
        product_portfolio=empty_product_portfolio,
        product_metric_fan=_metric_fan,
        product_rollout=_rollout,
        scenario_set_run=scenario_set_run,
    )
    response = TestClient(app).post("/api/scenario_sets/run", json={"scenario_set_id": "missing_required_fields"})

    assert response.status_code == 422
    assert not called


if __name__ == "__main__":
    pytest_bazel.main()
