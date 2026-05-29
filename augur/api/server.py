"""API-only Augur HTTP server: builds services from config and wires FastAPI routes."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from augur.api.calibration_wire import (
    CALIBRATION_FAN_PERCENTILES,
    CalibrationCatalogInfo,
    CalibrationCatalogsResponse,
    CalibrationRunRequest,
    CalibrationRunResponse,
)
from augur.api.casing import plain_json
from augur.api.catalog import build_bootstrap_payload
from augur.api.config import CalibrationCatalogConfig, Config, load_augur_config, resolve_augur_config_path
from augur.api.deployment import build_deployment_info
from augur.api.schemas import ApiModel
from augur.calibration.calibration import mark_fan, run_calibration, sample_private_equity_bundle
from augur.calibration.catalog import MarketCatalog
from augur.model.exogenous import Sampler
from augur.product.portfolio import product_portfolio_response
from augur.product.scenarios import resolve_primary_agent_id, sim_locations_from_config
from augur.product.service import ProductService
from augur.product.wire import MetricFanRequest, RolloutRequest


@dataclass(frozen=True)
class LoadedCalibrationCatalog:
    """A registered calibration catalog config paired with its parsed `MarketCatalog`."""

    config: CalibrationCatalogConfig
    catalog: MarketCatalog


@dataclass(frozen=True)
class ApiServerConfig:
    augur_config: Config
    exogenous_models: dict[str, Sampler]
    # Calibration catalogs parsed at startup (id -> config + parsed catalog). Empty when the
    # deployment registers no `calibration_catalogs`.
    calibration_catalogs: dict[str, LoadedCalibrationCatalog] = field(default_factory=dict)


def create_app(config: ApiServerConfig) -> FastAPI:
    augur_config = config.augur_config
    bootstrap = build_bootstrap_payload(augur_config)
    deployment_info = build_deployment_info()
    product_service = ProductService(
        portfolio=augur_config.portfolio,
        initial_cash_usd=float(augur_config.snapshot.cash_usd),
        primary_agent_id=resolve_primary_agent_id(augur_config),
        known_location_ids=frozenset(location.id for location in bootstrap.locations),
        locations=sim_locations_from_config(augur_config.locations),
        properties_by_id={property_.id: property_ for property_ in bootstrap.properties},
        exogenous_models=config.exogenous_models,
        max_rollout_samples=augur_config.max_rollout_samples,
    )

    app = FastAPI(title="Augur scenario API")
    no_store = {"cache-control": "no-store"}

    def error(status_code: int, detail: Any) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": detail}, headers=no_store)

    def payload(value: Any) -> JSONResponse:
        return JSONResponse(content=plain_json(value), headers=no_store)

    app.add_exception_handler(RequestValidationError, lambda request, exc: error(422, exc.errors()))
    app.add_exception_handler(ValidationError, lambda request, exc: error(422, exc.errors()))
    app.add_exception_handler(KeyError, lambda request, exc: error(400, str(exc)))
    app.add_exception_handler(ValueError, lambda request, exc: error(400, str(exc)))

    @app.get("/api/bootstrap")
    def bootstrap_house() -> JSONResponse:
        return payload(bootstrap)

    @app.get("/api/deployment")
    def deployment() -> JSONResponse:
        return payload(deployment_info)

    @app.get("/api/product/portfolio")
    def product_portfolio_snapshot() -> JSONResponse:
        return payload(product_portfolio_response(snapshot=augur_config.snapshot, portfolio=augur_config.portfolio))

    @app.get("/api/exogenous_presets")
    def exogenous_presets() -> JSONResponse:
        return payload(
            {"presets": sorted(augur_config.exogenous_presets), "default": augur_config.default_exogenous_preset_id}
        )

    @app.post("/api/product/projections/metric_fan")
    def product_projection_metric_fan(request: MetricFanRequest) -> JSONResponse:
        return payload(product_service.metric_fan(request))

    @app.post("/api/product/projections/rollout")
    def product_projection_rollout(request: RolloutRequest) -> JSONResponse:
        return payload(product_service.rollout(request))

    def calibration_payload(value: ApiModel) -> JSONResponse:
        # Calibration responses carry `date` fields (CalibrationResult.as_of,
        # resolution_deadline), which the stdlib JSON encoder behind JSONResponse
        # cannot serialize. Dump in JSON mode (dates -> ISO strings) first, keeping the
        # same snake_case + drop-None wire convention as `payload`.
        return JSONResponse(content=value.model_dump(mode="json", exclude_none=True), headers=no_store)

    @app.get("/api/calibration/catalogs")
    def calibration_catalogs() -> JSONResponse:
        return calibration_payload(
            CalibrationCatalogsResponse(
                catalogs=tuple(
                    CalibrationCatalogInfo(
                        id=catalog_id,
                        label=loaded.config.label or catalog_id,
                        issuer=loaded.config.issuer,
                        default_preset_id=loaded.config.default_preset_id,
                    )
                    for catalog_id, loaded in sorted(config.calibration_catalogs.items())
                )
            )
        )

    @app.post("/api/calibration/run")
    def calibration_run(request: CalibrationRunRequest) -> JSONResponse:
        # KeyError on either lookup -> 400 via the registered handler.
        loaded = config.calibration_catalogs[request.catalog_id]
        model = config.exogenous_models[request.preset_id]
        issuer = loaded.config.issuer
        rollout_seeds = tuple(range(request.seed, request.seed + request.rollouts))
        # One rollout drives both the market scoring and the issuer mark fan.
        bundle = sample_private_equity_bundle(
            model, issuer=issuer, horizon_months=request.horizon_months, rollout_seeds=rollout_seeds
        )
        result = run_calibration(
            model,
            loaded.catalog,
            issuer=issuer,
            horizon_months=request.horizon_months,
            rollout_seeds=rollout_seeds,
            live=request.live,
            bundle=bundle,
        )
        fan = mark_fan(
            bundle,
            issuer=issuer,
            rollout_count=request.rollouts,
            horizon_months=request.horizon_months,
            percentiles=CALIBRATION_FAN_PERCENTILES,
        )
        return calibration_payload(
            CalibrationRunResponse(
                catalog_id=request.catalog_id, preset_id=request.preset_id, result=result, mark_fan=fan
            )
        )

    @app.api_route("/api/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def unknown_api(full_path: str) -> JSONResponse:
        return error(404, f"unknown API endpoint: /api/{full_path}")

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok\n", headers=no_store)

    return app


def create_app_from_augur_config(augur_config: Config) -> FastAPI:
    exogenous_models: dict[str, Sampler] = {
        preset_id: cast(Sampler, provider.realize_model())
        for preset_id, provider in augur_config.exogenous_presets.items()
    }
    calibration_catalogs = {
        catalog_id: LoadedCalibrationCatalog(config=catalog, catalog=MarketCatalog.from_yaml(catalog.catalog_path))
        for catalog_id, catalog in augur_config.calibration_catalogs.items()
    }
    return create_app(
        ApiServerConfig(
            augur_config=augur_config,
            exogenous_models=exogenous_models,
            calibration_catalogs=calibration_catalogs,
        )
    )


def _add_server_args(parser: argparse.ArgumentParser, *, api_only_help: str) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--api-only", action="store_true", help=api_only_help)


def build_server_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Augur backend API.")
    _add_server_args(parser, api_only_help="Accepted for deployment wrappers; this target is already API-only.")
    return parser


def build_configured_server_arg_parser(
    *,
    description: str = "Serve the Augur backend API.",
    api_only_help: str = "Accepted for deployment wrappers; this target is already API-only.",
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--config", help="Path to Config YAML. Defaults to $AUGUR_CONFIG_PATH or /etc/augur/config.yaml."
    )
    _add_server_args(parser, api_only_help=api_only_help)
    return parser


def _run_server_with_args(*, augur_config: Config, args: argparse.Namespace) -> int:
    app = create_app_from_augur_config(augur_config)
    return run_app(app=app, augur_config=augur_config, host=args.host, port=args.port)


def run_app(*, app: FastAPI, augur_config: Config, host: str, port: int) -> int:
    print(f"serving Augur API on http://{host}:{port}")
    print(
        f"exogenous presets: {sorted(augur_config.exogenous_presets)} "
        f"(default: {augur_config.default_exogenous_preset_id})"
    )
    uvicorn.run(app, host=host, port=port)
    return 0


def run_server(*, augur_config: Config, argv: list[str] | None = None) -> int:
    return _run_server_with_args(augur_config=augur_config, args=build_server_arg_parser().parse_args(argv))


def run_configured_server(*, argv: list[str] | None = None) -> int:
    args = build_configured_server_arg_parser().parse_args(argv)
    config_path = Path(args.config).resolve() if args.config else resolve_augur_config_path()
    return _run_server_with_args(augur_config=load_augur_config(config_path), args=args)


def main(argv: list[str] | None = None) -> int:
    return run_configured_server(argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
