"""Generic augur HTTP server. A deployment-side wrapper (e.g. gaffer's
serve.py) provides the `AugurConfig`, bundle source, and market config
path, then calls `run_server(...)`."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from augur.api.backend import AugurBackend, AugurBackendRuntimeConfig
from augur.api.config import AugurConfig
from augur.core.backend import StaticPathResolver, create_augur_backend_app
from augur.core.market_bundle import FlatMarketBundleProvider, MarketBundleProvider, SimpleMarketBundleProvider
from augur.model.macro_market_bundle_provider import MacroMarketBundleProvider
from augur.model.markets.registry import LABELS

_BUILT_IN_PROVIDER_LABELS = ("noop", "simple")


@dataclass(frozen=True)
class StaticBundle:
    """Serve the React bundle from `dist_dir` alongside the API."""

    dist_dir: Path


@dataclass(frozen=True)
class ApiOnly:
    """Serve only JSON API routes; static assets are external."""


BundleSource = StaticBundle | ApiOnly


@dataclass(frozen=True)
class AugurServerConfig:
    augur_config: AugurConfig
    market_bundle_provider: MarketBundleProvider
    default_rollout_samples: int
    max_rollout_samples: int
    bundle: BundleSource


def _make_provider(
    args: argparse.Namespace, augur_config: AugurConfig, default_market_config_path: Path
) -> MarketBundleProvider:
    if args.provider == "noop":
        return FlatMarketBundleProvider()
    if args.provider == "simple":
        return SimpleMarketBundleProvider()
    # MacroMarketBundleProvider currently uses one concentrated holding's current
    # valuation to populate private-equity source metadata.
    holdings = augur_config.snapshot.concentrated_holdings
    if len(holdings) != 1:
        raise ValueError(f"expected exactly one concentrated holding for the macro provider; got {len(holdings)}")
    market_config_path = Path(args.market_config).resolve() if args.market_config else default_market_config_path
    return MacroMarketBundleProvider.for_label(
        args.provider, config_path=market_config_path, current_private_equity_price_usd=holdings[0].fmv_usd_per_unit
    )


def _static_path_resolver(bundle: BundleSource) -> StaticPathResolver | None:
    if not isinstance(bundle, StaticBundle):
        return None
    dist_dir = bundle.dist_dir

    def resolve(full_path: str) -> Path:
        # Strip absolute/traversal paths to a sentinel that will 404 in
        # FastAPI's FileResponse. Unknown SPA routes fall back to index.html so
        # the React router can take over.
        rel = "index.html" if full_path in ("", "/") else full_path.lstrip("/")
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            return dist_dir / "__forbidden__"
        candidate = dist_dir / relative
        if candidate.exists():
            return candidate
        if candidate.suffix:
            return candidate
        return dist_dir / "index.html"

    return resolve


def create_app(config: AugurServerConfig):
    backend = AugurBackend(
        augur_config=config.augur_config,
        runtime_config=AugurBackendRuntimeConfig(
            market_bundle_provider=config.market_bundle_provider,
            default_rollout_samples=config.default_rollout_samples,
            max_rollout_samples=config.max_rollout_samples,
        ),
    )
    return create_augur_backend_app(
        title="Augur scenario API",
        static_path=_static_path_resolver(config.bundle),
        bootstrap=backend.bootstrap_payload,
        scenario_set_run=backend.run_scenario_set_for_request_body,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the combined property-first Augur backend API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--market-config", help="Path to the market model config JSON.")
    parser.add_argument(
        "--api-only", action="store_true", help="Serve only JSON API routes; static assets are external."
    )
    parser.add_argument(
        "--provider",
        choices=(*_BUILT_IN_PROVIDER_LABELS, *LABELS),
        default="vecm",
        help="Market provider: built-in noop/simple or a macro model provider from augur.model.markets.registry.",
    )
    parser.add_argument("--dist-dir", help="Override the prebuilt frontend bundle directory.")
    parser.add_argument("--rollout-samples", type=int, default=None)
    parser.add_argument("--max-rollout-samples", type=int, default=2048)
    return parser


def _resolve_bundle(
    args: argparse.Namespace, default_bundle: BundleSource, parser: argparse.ArgumentParser
) -> BundleSource:
    if args.api_only:
        return ApiOnly()
    if args.dist_dir:
        return StaticBundle(dist_dir=Path(args.dist_dir).resolve())
    if isinstance(default_bundle, ApiOnly):
        parser.error("--dist-dir is required when the deployment provides no bundle and --api-only is not set")
    return default_bundle


def run_server(
    *, augur_config: AugurConfig, bundle: BundleSource, default_market_config_path: Path, argv: list[str] | None = None
) -> int:
    """Run the Augur HTTP server with the supplied AugurConfig and bundle source.

    Deployment-side entry points (e.g. gaffer's `serve.py`) resolve their
    runfile paths and pass them in as a `StaticBundle` (default) or `ApiOnly`;
    this module is module-agnostic and never references `_main/` directly.
    CLI args drive transport and market-provider choice; `--api-only` overrides
    the supplied default; `--dist-dir` overrides the default `StaticBundle`.
    AugurConfig drives everything user-specific."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    market_bundle_provider = _make_provider(args, augur_config, default_market_config_path)
    server_config = AugurServerConfig(
        augur_config=augur_config,
        market_bundle_provider=market_bundle_provider,
        default_rollout_samples=args.rollout_samples or augur_config.default_rollout_samples,
        max_rollout_samples=args.max_rollout_samples,
        bundle=_resolve_bundle(args, bundle, parser),
    )
    app = create_app(server_config)
    print(f"serving Augur on http://{args.host}:{args.port}")
    print(f"market provider: {args.provider}")
    match server_config.bundle:
        case StaticBundle(dist_dir=dist_dir):
            print(f"static bundle: {dist_dir}")
        case ApiOnly():
            print("static bundle: disabled")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0
