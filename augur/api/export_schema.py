"""Export the Augur API OpenAPI schema to stdout."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from augur.api.bootstrap import BootstrapResponse
from augur.api.calibration_wire import CalibrationCatalogsResponse, CalibrationRunRequest, CalibrationRunResponse
from augur.api.deployment import DeploymentInfo
from augur.product.portfolio import ProductPortfolioResponse
from augur.product.wire import MetricFanRequest, MetricFanResponse, RolloutRequest, RolloutResponse


def create_schema_app() -> FastAPI:
    app = FastAPI(title="Augur scenario API")

    @app.get("/api/bootstrap", response_model=BootstrapResponse)
    def bootstrap() -> BootstrapResponse:
        raise RuntimeError("schema-only route")

    @app.get("/api/deployment", response_model=DeploymentInfo)
    def deployment() -> DeploymentInfo:
        raise RuntimeError("schema-only route")

    @app.get("/api/product/portfolio", response_model=ProductPortfolioResponse)
    def product_portfolio() -> ProductPortfolioResponse:
        raise RuntimeError("schema-only route")

    @app.post("/api/product/projections/metric_fan", response_model=MetricFanResponse)
    def product_projection_metric_fan(request: MetricFanRequest) -> MetricFanResponse:
        raise RuntimeError("schema-only route")

    @app.post("/api/product/projections/rollout", response_model=RolloutResponse)
    def product_projection_rollout(request: RolloutRequest) -> RolloutResponse:
        raise RuntimeError("schema-only route")

    @app.get("/api/calibration/catalogs", response_model=CalibrationCatalogsResponse)
    def calibration_catalogs() -> CalibrationCatalogsResponse:
        raise RuntimeError("schema-only route")

    @app.post("/api/calibration/run", response_model=CalibrationRunResponse)
    def calibration_run(request: CalibrationRunRequest) -> CalibrationRunResponse:
        raise RuntimeError("schema-only route")

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok\n"

    return app


def main() -> None:
    print(json.dumps(create_schema_app().openapi(), indent=2))


if __name__ == "__main__":
    main()
