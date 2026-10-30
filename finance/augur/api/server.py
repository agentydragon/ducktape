"""API-only Augur HTTP server: builds services from config and wires FastAPI routes."""

from __future__ import annotations

import argparse
import os
import socket
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import uvicorn
import yaml
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError
from starlette.types import Lifespan

from finance.augur.api.calibration_wire import (
    CALIBRATION_FAN_PERCENTILES,
    CalibrationRunRequest,
    CalibrationRunResponse,
    sanity_band_to_wire,
)
from finance.augur.api.casing import plain_json
from finance.augur.api.catalog import build_calibration_info, build_catalog, build_settings
from finance.augur.api.config import CalibrationCatalogConfig, Config, load_augur_config, resolve_augur_config_path
from finance.augur.api.deployment import DeploymentInfo, build_deployment_info
from finance.augur.api.portfolio_sources import ResolvedPortfolioSources, resolve_portfolio_sources
from finance.augur.api.product_service import build_product_service
from finance.augur.api.wire import CalibrationInfo, CatalogResponse, SettingsResponse
from finance.augur.calibration.calibration import build_anchored_level_paths, mark_fan, run_calibration
from finance.augur.calibration.catalog import MarketCatalog
from finance.augur.calibration.default_clients import default_price_clients
from finance.augur.calibration.macro_anchors import resolve_anchors
from finance.augur.calibration.platform import PriceClient
from finance.augur.model.exogenous import ExogenousSamplingRequest, Sampler, level_series_request_channels
from finance.augur.model.private_equity_bundle import PrivateEquityFloatChannel
from finance.augur.model.series import IssuerId, LevelSeriesKey, parse_level_series_key
from finance.augur.product.portfolio import ProductPortfolioResponse, product_portfolio_response
from finance.augur.product.service import ProductService
from finance.augur.product.wire import (
    MetricFanResponse,
    ProductProjectionRequest,
    ProductProjectionResponse,
    ProjectionSamplingRequest,
    RolloutRequest,
    RolloutResponse,
    TerminalDistributionResponse,
)
from finance.augur.sim.money import USD
from finance.augur.x.models.sample_sanity import SampleSanitySpec, evaluate_sample_checks, partition_spec_coverage
from finance.budget.service import BudgetService
from finance.budget.wire import (
    BudgetSnapshotRequest,
    BudgetSnapshotResponse,
    BudgetSummaryCsvRequest,
    BudgetTransactionsRequest,
    BudgetTransactionsResponse,
)
from finance.evidence.markets import Platform
from finance.plaid.db.models import async_session_factory


@dataclass(frozen=True)
class LoadedCalibrationCatalog:
    """The configured calibration catalog config paired with its parsed `MarketCatalog`.

    `sample_sanity_spec` is the parsed `SampleSanitySpec` when the deployment configures a
    `sample_sanity_path`, else None (the feature is simply absent). Only its `*_checks` are
    consumed; the bands are scored against the live preset model the calibration endpoint runs.
    """

    config: CalibrationCatalogConfig
    catalog: MarketCatalog
    sample_sanity_spec: SampleSanitySpec | None = None


PriceClientsProvider = AbstractAsyncContextManager[dict[Platform, PriceClient]]


@asynccontextmanager
async def static_price_clients(clients: dict[Platform, PriceClient]) -> AsyncIterator[dict[Platform, PriceClient]]:
    """A no-lifecycle price-client provider over an already-built mapping (tests, product-only apps)."""
    yield clients


@dataclass(frozen=True)
class ApiServerConfig:
    augur_config: Config
    models: dict[str, Sampler]
    # Prediction-market price sources for `/api/calibration/run` (per-market YES probs), as an
    # async context manager entered once for the app's lifetime (server lifespan): it yields the
    # `{Platform: client}` map and owns any provider lifecycle. Use `default_price_clients()` in
    # production for evidence-checkout readers, `static_price_clients(...)` for already-built
    # hermetic clients in tests.
    price_clients: PriceClientsProvider
    # The deployment's calibration catalog parsed into a `MarketCatalog` at startup. None only
    # when the app is assembled directly without loading it (e.g. a product-only TestClient);
    # `/api/calibration/run` then 400s. `create_app_from_augur_config` always loads it.
    calibration_catalog: LoadedCalibrationCatalog | None = None
    # Holds the plaid mirror session factory and the budget config it operates on. Constructed
    # once at startup (so the asyncpg connection pool + SSL handshake are paid once, not per
    # request) and shared across `/api/budget/*` calls. None when no `budget` is configured;
    # the endpoints then 400.
    budget_service: BudgetService | None = None


@dataclass(frozen=True)
class AppServices:
    """The deployment state route handlers read, resolved once from an `ApiServerConfig` by `build_services`
    (price clients are separate: the `create_app` lifespan enters them on the serving loop).

    `create_app` binds it to the app as `app.state.services`; handlers resolve it per request via
    `Services`, so `build_app` registers the production route graph without a deployment behind it
    (how `export_schema` reaches `app.openapi()` without acquiring any runtime resource)."""

    config: ApiServerConfig
    portfolio: ResolvedPortfolioSources
    catalog: CatalogResponse
    settings: SettingsResponse
    calibration_info: CalibrationInfo
    deployment_info: DeploymentInfo
    product_service: ProductService


def build_services(config: ApiServerConfig) -> AppServices:
    """Acquire the deployment's runtime resources: the property catalog, the portfolio sources
    (Plaid is read here, at startup) and the product service over the realized models."""
    augur_config = config.augur_config
    catalog = build_catalog(augur_config)
    loaded_calibration = config.calibration_catalog
    return AppServices(
        config=config,
        portfolio=resolve_portfolio_sources(augur_config),
        catalog=catalog,
        settings=build_settings(augur_config),
        calibration_info=build_calibration_info(
            loaded_calibration.catalog if loaded_calibration is not None else None, augur_config.calibration_catalog
        ),
        deployment_info=build_deployment_info(),
        product_service=build_product_service(augur_config, config.models, catalog=catalog),
    )


def _services(request: Request) -> AppServices:
    return cast(AppServices, request.app.state.services)


def _price_clients(request: Request) -> dict[Platform, PriceClient]:
    # Entered by the `create_app` lifespan: present inside the serving loop or an entered TestClient.
    return cast(dict[Platform, PriceClient], request.app.state.price_clients)


Services = Annotated[AppServices, Depends(_services)]
PriceClients = Annotated[dict[Platform, PriceClient], Depends(_price_clients)]


def build_app(*, lifespan: Lifespan[FastAPI] | None) -> FastAPI:
    """The production route graph and error handling, unbound to any deployment.

    Handlers read `AppServices` off `app.state.services` at request time, so registering every
    route (which fixes the OpenAPI document) acquires no runtime resource. `create_app` binds a
    deployment and the lifespan entering its price clients; `export_schema` passes `lifespan=None`
    and only calls `.openapi()`."""
    app = FastAPI(title="Augur scenario API", lifespan=lifespan)
    no_store = {"cache-control": "no-store"}

    def error(status_code: int, detail: Any) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": detail}, headers=no_store)

    def payload(value: Any, *, exclude_none: bool = True) -> JSONResponse:
        return JSONResponse(content=plain_json(value, exclude_none=exclude_none), headers=no_store)

    app.add_exception_handler(RequestValidationError, lambda request, exc: error(422, exc.errors()))
    app.add_exception_handler(ValidationError, lambda request, exc: error(422, exc.errors()))
    app.add_exception_handler(KeyError, lambda request, exc: error(400, str(exc)))
    app.add_exception_handler(ValueError, lambda request, exc: error(400, str(exc)))

    # Routes return a `JSONResponse` directly (custom snake_case + drop-None wire), so FastAPI
    # passes the Response through untouched. `response_model=` is purely for the OpenAPI document
    # `augur.api.export_schema` dumps to drive the frontend's Zod/TS codegen.

    @app.get("/api/catalog", response_model=CatalogResponse)
    def catalog_route(services: Services) -> JSONResponse:
        return payload(services.catalog)

    @app.get("/api/settings", response_model=SettingsResponse)
    def settings_route(services: Services) -> JSONResponse:
        return payload(services.settings)

    @app.get("/api/calibration", response_model=CalibrationInfo)
    def calibration_info_route(services: Services) -> JSONResponse:
        return payload(services.calibration_info)

    @app.get("/api/deployment", response_model=DeploymentInfo)
    def deployment(services: Services) -> JSONResponse:
        return payload(services.deployment_info)

    @app.get("/api/product/portfolio", response_model=ProductPortfolioResponse)
    def product_portfolio_snapshot(services: Services) -> JSONResponse:
        return payload(
            product_portfolio_response(
                snapshot=services.portfolio.snapshot,
                portfolio=services.portfolio.portfolio,
                tlh_portfolios=services.portfolio.tlh_portfolios,
                currency=USD,
            )
        )

    @app.post("/api/product/projections/metric_fan", response_model=MetricFanResponse)
    def product_projection_metric_fan(request: ProjectionSamplingRequest, services: Services) -> JSONResponse:
        return payload(services.product_service.metric_fan(request))

    @app.post("/api/product/projections/terminal_distribution", response_model=TerminalDistributionResponse)
    def product_projection_terminal_distribution(
        request: ProjectionSamplingRequest, services: Services
    ) -> JSONResponse:
        return payload(services.product_service.terminal_distribution(request))

    @app.post("/api/product/projections/summary", response_model=ProductProjectionResponse)
    def product_projection_summary(request: ProductProjectionRequest, services: Services) -> JSONResponse:
        return payload(services.product_service.projection_summary(request))

    @app.post("/api/product/projections/rollout", response_model=RolloutResponse)
    def product_projection_rollout(request: RolloutRequest, services: Services) -> JSONResponse:
        return payload(services.product_service.rollout(request))

    @app.post("/api/calibration/run", response_model=CalibrationRunResponse)
    async def calibration_run(
        request: CalibrationRunRequest, services: Services, price_clients: PriceClients
    ) -> JSONResponse:
        config = services.config
        loaded = config.calibration_catalog
        if loaded is None:
            return error(400, "calibration catalog is not loaded for this server instance")
        preset_id = request.preset_id or config.augur_config.default_model_id
        # KeyError on the preset lookup -> 400 via the registered handler.
        model = config.models[preset_id]
        # The catalog self-describes its PE issuers; the run + fans cover the union the preset emits.
        emit_issuers = sorted(
            IssuerId(issuer)
            for issuer in loaded.catalog.referenced_issuers()
            if IssuerId(issuer) in model.emittable_private_equity_issuers()
        )
        spec = loaded.sample_sanity_spec
        rollout_seeds = tuple(range(request.seed, request.seed + request.rollouts))
        # Sample one full bundle that drives the market scoring, the issuer mark fan, AND the
        # sample-sanity bands — still a single rollout. The PE bundle goes to `run_calibration`/
        # `mark_fan` (preserving today's behavior); the level series the sanity spec wants are
        # added on top, partitioned by what the deployment's preset can actually emit so a
        # band declared against an unmodeled series renders as "not modeled" rather than 400-ing.
        # TODO: sample at max(request.horizon_months, max band month in spec) and drop the
        # `skipped` status — currently a user-picked short horizon silently marks longer spec
        # bands as skipped, which the sample-sanity team would rather always evaluate. Once
        # that lands, the calibration tab's horizon control no longer drives sampling and can
        # either move to the Product tab (sampling-only) or stay as a pure chart-x-axis zoom.
        spec_modeled_level: frozenset[LevelSeriesKey] = frozenset()
        spec_modeled_pe: frozenset[IssuerId] = frozenset()
        unmodeled_level: frozenset[LevelSeriesKey] = frozenset()
        unmodeled_pe: frozenset[IssuerId] = frozenset()
        if spec is not None:
            spec_modeled_level, spec_modeled_pe, unmodeled_level, unmodeled_pe = partition_spec_coverage(spec, model)
        # The macro markets / bucket families the catalog scores need their level series sampled too.
        # Request only the ones the preset can emit; markets on the rest surface as `unmodeled`.
        catalog_level = {parse_level_series_key(wire) for wire in loaded.catalog.referenced_level_series()}
        wanted_level = spec_modeled_level | (catalog_level & model.emittable_level_keys())
        sampling_request = ExogenousSamplingRequest(
            horizon_months=request.horizon_months,
            rollout_seeds=rollout_seeds,
            required_private_equity_issuers=frozenset(emit_issuers) | spec_modeled_pe,
            **level_series_request_channels(wanted_level),
        )
        sampled = model.sample(sampling_request)
        bundle = sampled.private_equity
        anchors = resolve_anchors(loaded.catalog)
        level_paths = build_anchored_level_paths(
            sampled,
            anchors=anchors.anchors,
            requested_wire_ids=loaded.catalog.referenced_level_series(),
            rollout_count=request.rollouts,
            horizon_months=request.horizon_months,
        )
        result = await run_calibration(
            loaded.catalog,
            horizon_months=request.horizon_months,
            rollout_seeds=rollout_seeds,
            price_clients=price_clients,
            bundle=bundle,
            level_paths=level_paths,
            inflation_history=anchors.inflation_history,
        )
        # One mark fan per scored issuer; valuation fan only for issuers whose opt-in channel is on.
        mark_fans = [
            mark_fan(
                bundle,
                issuer=issuer,
                rollout_count=request.rollouts,
                horizon_months=request.horizon_months,
                percentiles=CALIBRATION_FAN_PERCENTILES,
            )
            for issuer in emit_issuers
        ]
        valuation_fans = [
            mark_fan(
                bundle,
                issuer=issuer,
                rollout_count=request.rollouts,
                horizon_months=request.horizon_months,
                percentiles=CALIBRATION_FAN_PERCENTILES,
                channel=PrivateEquityFloatChannel.COMPANY_VALUATION_USD,
            )
            for issuer in emit_issuers
            if bool(
                (
                    bundle.issuer_float_matrix(
                        issuer,
                        PrivateEquityFloatChannel.COMPANY_VALUATION_USD,
                        rollout_count=request.rollouts,
                        horizon_months=request.horizon_months,
                    )
                    > 0.0
                ).any()
            )
        ]
        sanity_bands = (
            [
                sanity_band_to_wire(band)
                for band in evaluate_sample_checks(
                    spec,
                    sampled,
                    rollout_count=request.rollouts,
                    horizon_months=request.horizon_months,
                    unmodeled_level_keys=unmodeled_level,
                    unmodeled_pe_issuers=unmodeled_pe,
                )
            ]
            if spec is not None
            else []
        )
        return payload(
            CalibrationRunResponse(
                preset_id=preset_id,
                result=result,
                mark_fans=mark_fans,
                valuation_fans=valuation_fans,
                sanity_bands=sanity_bands,
            )
        )

    @app.post("/api/budget/snapshot", response_model=BudgetSnapshotResponse)
    async def budget_snapshot(request: BudgetSnapshotRequest, services: Services) -> JSONResponse:
        budget_service = services.config.budget_service
        if budget_service is None:
            return error(400, "no budget config for this deployment")
        result = await budget_service.build_snapshot(window=request.window)
        return payload(result, exclude_none=False)

    @app.post("/api/budget/transactions", response_model=BudgetTransactionsResponse)
    async def budget_transactions(request: BudgetTransactionsRequest, services: Services) -> JSONResponse:
        budget_service = services.config.budget_service
        if budget_service is None:
            return error(400, "no budget config for this deployment")
        result = await budget_service.list_transactions_in_bucket(bucket_id=request.bucket_id, window=request.window)
        return payload(result, exclude_none=False)

    @app.post("/api/budget/snapshot.csv", include_in_schema=False)
    async def budget_snapshot_csv(request: BudgetSummaryCsvRequest, services: Services) -> Response:
        budget_service = services.config.budget_service
        if budget_service is None:
            return error(400, "no budget config for this deployment")
        result = await budget_service.build_snapshot_csv(window=request.window, adjustments=request.adjustments)
        return Response(content=result, media_type="text/csv; charset=utf-8", headers=no_store)

    @app.post("/api/budget/transactions.csv", include_in_schema=False)
    async def budget_transactions_csv(request: BudgetTransactionsRequest, services: Services) -> Response:
        budget_service = services.config.budget_service
        if budget_service is None:
            return error(400, "no budget config for this deployment")
        result = await budget_service.build_transactions_csv(bucket_id=request.bucket_id, window=request.window)
        return Response(content=result, media_type="text/csv; charset=utf-8", headers=no_store)

    # The health check is not part of the typed wire contract, so keep it out of the
    # OpenAPI document `export_schema` dumps (no Zod/TS codegen noise). Unknown API routes
    # get FastAPI's default 404; nginx serves the SPA, so the app needs no static catch-all.
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok\n", headers=no_store)

    return app


def create_app(config: ApiServerConfig) -> FastAPI:
    services = build_services(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Enter the price-client provider once for the process so any provider-owned resources are
        # bound to the serving loop and closed on shutdown. `/api/calibration/run` reads them off
        # `app.state` through `PriceClients`.
        async with config.price_clients as price_clients:
            app.state.price_clients = price_clients
            yield

    app = build_app(lifespan=lifespan)
    app.state.services = services
    return app


def _load_sample_sanity_spec(path: Path | None) -> SampleSanitySpec | None:
    """Parse the deployment's `SampleSanitySpec` YAML, or None when unconfigured.

    Consumed only for its `*_checks`; the bands are scored against the live preset model the
    calibration endpoint runs (the spec carries no model of its own)."""
    if path is None:
        return None
    return SampleSanitySpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def create_app_from_augur_config(augur_config: Config, *, price_clients: PriceClientsProvider) -> FastAPI:
    """Build the app from a `Config`.

    `price_clients` is the live prediction-market price-source provider for `/api/calibration/run`,
    entered once for the app's lifetime (server lifespan). Production passes `default_price_clients()`;
    tests pass `static_price_clients(...)` over hermetic clients."""
    models: dict[str, Sampler] = {
        preset_id: cast(Sampler, provider.realize_model()) for preset_id, provider in augur_config.models.items()
    }
    catalog_config = augur_config.calibration_catalog
    calibration_catalog = LoadedCalibrationCatalog(
        config=catalog_config,
        catalog=MarketCatalog.from_yaml(catalog_config.catalog_path),
        sample_sanity_spec=_load_sample_sanity_spec(catalog_config.sample_sanity_path),
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
    parser.add_argument(
        "--fd", type=int, help="Serve on this inherited listening socket instead of binding --host/--port."
    )
    parser.add_argument("--api-only", action="store_true", help=api_only_help)


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
    app = create_app_from_augur_config(augur_config, price_clients=default_price_clients())
    return run_app(app=app, augur_config=augur_config, host=args.host, port=args.port, fd=args.fd)


def run_app(*, app: FastAPI, augur_config: Config, host: str, port: int, fd: int | None = None) -> int:
    """Serve `app` on `host`:`port`, or on the inherited listening socket `fd` when supplied."""
    config = uvicorn.Config(app, host=host, port=port)
    with config.bind_socket() if fd is None else socket.socket(fileno=fd) as sock:
        bound_host, bound_port = sock.getsockname()[:2]
        print(f"serving Augur API on http://{bound_host}:{bound_port}")
        print(f"models: {sorted(augur_config.models)} (default: {augur_config.default_model_id})")
        uvicorn.Server(config).run(sockets=[sock])
    return 0


def run_configured_server(*, argv: list[str] | None = None) -> int:
    args = build_configured_server_arg_parser().parse_args(argv)
    config_path = Path(args.config).resolve() if args.config else resolve_augur_config_path()
    return _run_server_with_args(augur_config=load_augur_config(config_path), args=args)


def main(argv: list[str] | None = None) -> int:
    return run_configured_server(argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
