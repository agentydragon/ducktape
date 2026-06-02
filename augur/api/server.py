"""API-only Augur HTTP server: builds services from config and wires FastAPI routes."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from augur.api.calibration_wire import (
    CALIBRATION_FAN_PERCENTILES,
    CalibrationRunRequest,
    CalibrationRunResponse,
    sanity_band_to_wire,
)
from augur.api.casing import plain_json
from augur.api.catalog import build_calibration_info, build_catalog, build_settings
from augur.api.config import CalibrationCatalogConfig, Config, load_augur_config, resolve_augur_config_path
from augur.api.deployment import DeploymentInfo, build_deployment_info
from augur.api.portfolio_sources import resolve_portfolio_sources
from augur.api.schemas import ApiModel
from augur.api.wire import CalibrationInfo, CatalogResponse, SettingsResponse
from augur.budget.service import BudgetService
from augur.budget.wire import (
    BudgetSnapshotRequest,
    BudgetSnapshotResponse,
    BudgetTransactionsRequest,
    BudgetTransactionsResponse,
)
from augur.calibration.calibration import mark_fan, run_calibration
from augur.calibration.catalog import MarketCatalog
from augur.calibration.kalshi import KalshiClient
from augur.calibration.manifold import ManifoldClient
from augur.calibration.platform import Platform, PriceClient
from augur.calibration.polymarket import PolymarketClient
from augur.model.exogenous import ExogenousSamplingRequest, Sampler, level_series_request_channels
from augur.model.private_equity_bundle import PrivateEquityFloatChannel
from augur.model.sample_sanity import SampleSanitySpec, evaluate_sample_checks
from augur.model.series import IssuerId
from augur.product.portfolio import ProductPortfolioResponse, product_portfolio_response
from augur.product.scenarios import resolve_primary_agent_id, sim_locations_from_config
from augur.product.service import ProductService
from augur.product.wire import MetricFanRequest, MetricFanResponse, RolloutRequest, RolloutResponse
from plaid_utils.schema import async_session_factory


@dataclass(frozen=True)
class LoadedCalibrationCatalog:
    """The configured calibration catalog config paired with its parsed `MarketCatalog`.

    `sample_sanity_spec` is the parsed `SampleSanitySpec` when the deployment configures a
    `sample_sanity_path`, else None (the feature is simply absent). Only its `*_checks` +
    `required_*` are consumed; its `provider_config_path` is never realized — the calibration
    endpoint reuses the live preset model.
    """

    config: CalibrationCatalogConfig
    catalog: MarketCatalog
    sample_sanity_spec: SampleSanitySpec | None = None


@dataclass(frozen=True)
class ApiServerConfig:
    augur_config: Config
    models: dict[str, Sampler]
    # Live prediction-market price sources for `/api/calibration/run` (per-market YES probs).
    # Maps each Platform to its client. Reused across requests, so the TTL caches serve the
    # calibration tab's rapid auto-refreshes.
    price_clients: dict[Platform, PriceClient]
    # The calibration catalog parsed at startup, or None when the deployment configures no
    # `calibration_catalog` (the `/api/calibration/run` endpoint then 400s).
    calibration_catalog: LoadedCalibrationCatalog | None = None
    # Holds the plaid mirror session factory and the budget config it operates on. Constructed
    # once at startup (so the asyncpg connection pool + SSL handshake are paid once, not per
    # request) and shared across `/api/budget/*` calls. None when no `budget` is configured;
    # the endpoints then 400.
    budget_service: BudgetService | None = None


def create_app(config: ApiServerConfig) -> FastAPI:
    augur_config = config.augur_config
    resolved_portfolio = resolve_portfolio_sources(augur_config)
    catalog = build_catalog(augur_config)
    settings = build_settings(augur_config)
    calibration_info = build_calibration_info(augur_config)
    deployment_info = build_deployment_info()
    product_service = ProductService(
        portfolio=resolved_portfolio.portfolio,
        initial_cash_usd=float(resolved_portfolio.snapshot.cash_usd),
        primary_agent_id=resolve_primary_agent_id(augur_config),
        known_location_ids=frozenset(location.id for location in catalog.locations),
        locations=sim_locations_from_config(augur_config.locations),
        properties_by_id={property_.id: property_ for property_ in catalog.properties},
        models=config.models,
        max_rollout_samples=augur_config.max_rollout_samples,
    )

    app = FastAPI(title="Augur scenario API")
    no_store = {"cache-control": "no-store"}

    def error(status_code: int, detail: Any) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": detail}, headers=no_store)

    def payload(value: Any) -> JSONResponse:
        return JSONResponse(content=plain_json(value), headers=no_store)

    app.add_exception_handler(RequestValidationError, lambda request, exc: error(422, exc.errors(include_input=False)))
    app.add_exception_handler(ValidationError, lambda request, exc: error(422, exc.errors(include_input=False)))
    app.add_exception_handler(KeyError, lambda request, exc: error(400, str(exc)))
    app.add_exception_handler(ValueError, lambda request, exc: error(400, str(exc)))

    # Routes return a `JSONResponse` directly (custom snake_case + drop-None wire), so FastAPI
    # passes the Response through untouched. `response_model=` is purely for the OpenAPI document
    # `augur.api.export_schema` dumps to drive the frontend's Zod/TS codegen.

    @app.get("/api/catalog", response_model=CatalogResponse)
    def catalog_route() -> JSONResponse:
        return payload(catalog)

    @app.get("/api/settings", response_model=SettingsResponse)
    def settings_route() -> JSONResponse:
        return payload(settings)

    # Null body (HTTP 200) when the deployment configures no `calibration_catalog`; the
    # calibration tab reads that as "no catalog" rather than erroring.
    @app.get("/api/calibration", response_model=CalibrationInfo | None)
    def calibration_info_route() -> JSONResponse:
        return payload(calibration_info)

    @app.get("/api/deployment", response_model=DeploymentInfo)
    def deployment() -> JSONResponse:
        return payload(deployment_info)

    @app.get("/api/product/portfolio", response_model=ProductPortfolioResponse)
    def product_portfolio_snapshot() -> JSONResponse:
        return payload(
            product_portfolio_response(snapshot=resolved_portfolio.snapshot, portfolio=resolved_portfolio.portfolio)
        )

    @app.post("/api/product/projections/metric_fan", response_model=MetricFanResponse)
    def product_projection_metric_fan(request: MetricFanRequest) -> JSONResponse:
        return payload(product_service.metric_fan(request))

    @app.post("/api/product/projections/rollout", response_model=RolloutResponse)
    def product_projection_rollout(request: RolloutRequest) -> JSONResponse:
        return payload(product_service.rollout(request))

    def calibration_payload(value: ApiModel) -> JSONResponse:
        # Calibration responses carry `date` fields (CalibrationResult.as_of,
        # resolution_deadline), which the stdlib JSON encoder behind JSONResponse
        # cannot serialize. Dump in JSON mode (dates -> ISO strings) first, keeping the
        # same snake_case + drop-None wire convention as `payload`.
        return JSONResponse(content=value.model_dump(mode="json", exclude_none=True), headers=no_store)

    @app.post("/api/calibration/run", response_model=CalibrationRunResponse)
    def calibration_run(request: CalibrationRunRequest) -> JSONResponse:
        loaded = config.calibration_catalog
        if loaded is None:
            return error(400, "no calibration_catalog configured for this deployment")
        preset_id = request.preset_id or config.augur_config.default_model_id
        # KeyError on the preset lookup -> 400 via the registered handler.
        model = config.models[preset_id]
        issuer = loaded.config.issuer
        spec = loaded.sample_sanity_spec
        rollout_seeds = tuple(range(request.seed, request.seed + request.rollouts))
        # Sample one full bundle that drives the market scoring, the issuer mark fan, AND the
        # sample-sanity bands — still a single rollout. The PE bundle goes to `run_calibration`/
        # `mark_fan` (preserving today's behavior); the level series the sanity spec needs are
        # requested here too. A spec asking for a series the preset can't produce raises -> 400.
        sampling_request = ExogenousSamplingRequest(
            horizon_months=request.horizon_months,
            rollout_seeds=rollout_seeds,
            required_private_equity_issuers=frozenset({IssuerId(issuer)}),
            **level_series_request_channels(frozenset(spec.required_level_series) if spec is not None else frozenset()),
        )
        sampled = model.sample(sampling_request)
        bundle = sampled.private_equity
        result = run_calibration(
            model,
            loaded.catalog,
            issuer=issuer,
            horizon_months=request.horizon_months,
            rollout_seeds=rollout_seeds,
            price_clients=config.price_clients,
            bundle=bundle,
        )
        fan = mark_fan(
            bundle,
            issuer=issuer,
            rollout_count=request.rollouts,
            horizon_months=request.horizon_months,
            percentiles=CALIBRATION_FAN_PERCENTILES,
        )
        valuation_matrix = bundle.issuer_float_matrix(
            issuer,
            PrivateEquityFloatChannel.COMPANY_VALUATION_USD,
            rollout_count=request.rollouts,
            horizon_months=request.horizon_months,
        )
        valuation_fan = (
            mark_fan(
                bundle,
                issuer=issuer,
                rollout_count=request.rollouts,
                horizon_months=request.horizon_months,
                percentiles=CALIBRATION_FAN_PERCENTILES,
                channel=PrivateEquityFloatChannel.COMPANY_VALUATION_USD,
            )
            if bool((valuation_matrix > 0.0).any())
            else None
        )
        sanity_bands = (
            [
                sanity_band_to_wire(band)
                for band in evaluate_sample_checks(
                    spec, sampled, rollout_count=request.rollouts, horizon_months=request.horizon_months
                )
            ]
            if spec is not None
            else []
        )
        return calibration_payload(
            CalibrationRunResponse(
                preset_id=preset_id, result=result, mark_fan=fan, valuation_fan=valuation_fan, sanity_bands=sanity_bands
            )
        )

    @app.post("/api/budget/snapshot", response_model=BudgetSnapshotResponse)
    async def budget_snapshot(request: BudgetSnapshotRequest) -> JSONResponse:
        if config.budget_service is None:
            return error(400, "no budget config for this deployment")
        # Snapshot rows carry `date` fields (months, lumpy.date) that the stdlib JSON
        # encoder behind JSONResponse can't serialize; dump in JSON mode (dates -> ISO
        # strings) like calibration_payload, same snake_case + drop-None wire convention.
        result = await config.budget_service.build_snapshot(months=request.months)
        return JSONResponse(content=result.model_dump(mode="json"), headers=no_store)

    @app.post("/api/budget/transactions", response_model=BudgetTransactionsResponse)
    async def budget_transactions(request: BudgetTransactionsRequest) -> JSONResponse:
        if config.budget_service is None:
            return error(400, "no budget config for this deployment")
        result = await config.budget_service.list_transactions_in_bucket(
            bucket_id=request.bucket_id, months=request.months
        )
        return JSONResponse(content=result.model_dump(mode="json"), headers=no_store)

    # The health check is not part of the typed wire contract, so keep it out of the
    # OpenAPI document `export_schema` dumps (no Zod/TS codegen noise). Unknown API routes
    # get FastAPI's default 404; nginx serves the SPA, so the app needs no static catch-all.
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok\n", headers=no_store)

    return app


def _load_sample_sanity_spec(path: Path | None) -> SampleSanitySpec | None:
    """Parse the deployment's `SampleSanitySpec` YAML, or None when unconfigured.

    Consumed only for its `*_checks` + `required_*`; `provider_config_path` is left unresolved
    (the calibration endpoint reuses the live preset model rather than realizing the spec's)."""
    if path is None:
        return None
    return SampleSanitySpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def create_app_from_augur_config(augur_config: Config, *, price_clients: dict[Platform, PriceClient]) -> FastAPI:
    """Build the app from a `Config`.

    `price_clients` is the live prediction-market price source map for
    `/api/calibration/run` (callers construct the appropriate clients)."""
    models: dict[str, Sampler] = {
        preset_id: cast(Sampler, provider.realize_model()) for preset_id, provider in augur_config.models.items()
    }
    catalog_config = augur_config.calibration_catalog
    calibration_catalog = (
        LoadedCalibrationCatalog(
            config=catalog_config,
            catalog=MarketCatalog.from_yaml(catalog_config.catalog_path),
            sample_sanity_spec=_load_sample_sanity_spec(catalog_config.sample_sanity_path),
        )
        if catalog_config is not None
        else None
    )
    budget_service: BudgetService | None = None
    if augur_config.budget is not None:
        db_url_env = augur_config.budget.source.database_url_env
        db_url = os.environ.get(db_url_env)
        if not db_url:
            raise RuntimeError(
                f"budget config is set but env var {db_url_env!r} is not -- "
                "point it at the plaid mirror postgresql URL or remove the `budget:` block."
            )
        # Engine + asyncpg connection pool built once at startup; reused across every
        # /api/budget/* request via BudgetService.session_factory.
        _, session_factory = async_session_factory(db_url)
        budget_service = BudgetService(config=augur_config.budget, session_factory=session_factory)
    server_config = ApiServerConfig(
        augur_config=augur_config,
        models=models,
        calibration_catalog=calibration_catalog,
        price_clients=price_clients,
        budget_service=budget_service,
    )
    return create_app(server_config)


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
    price_clients: dict[Platform, PriceClient] = {
        Platform.MANIFOLD: ManifoldClient(),
        Platform.POLYMARKET: PolymarketClient(),
        Platform.KALSHI: KalshiClient(),
    }
    app = create_app_from_augur_config(augur_config, price_clients=price_clients)
    return run_app(app=app, augur_config=augur_config, host=args.host, port=args.port)


def run_app(*, app: FastAPI, augur_config: Config, host: str, port: int) -> int:
    print(f"serving Augur API on http://{host}:{port}")
    print(f"models: {sorted(augur_config.models)} (default: {augur_config.default_model_id})")
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
