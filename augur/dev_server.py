"""Combined Augur dev server for the public fixture app."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from augur.api.config import Config, load_augur_config, resolve_augur_config_path
from augur.api.server import (
    PriceClientsProvider,
    build_configured_server_arg_parser,
    create_app_from_augur_config,
    run_app,
)
from augur.calibration.default_clients import default_price_clients
from util.bazel.runfiles import get_required_path

_AUGUR_BUNDLE_INDEX_RUNFILE_ENV_VAR = "AUGUR_BUNDLE_INDEX_RUNFILE"
StaticPathResolver = Callable[[str], Path]


def _static_path_resolver(dist_dir: Path) -> StaticPathResolver:
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


def _fixture_bundle_static_path() -> StaticPathResolver:
    bundle_index_runfile = os.environ[_AUGUR_BUNDLE_INDEX_RUNFILE_ENV_VAR]
    return _static_path_resolver(get_required_path(bundle_index_runfile).parent)


def _mount_static_bundle(app: FastAPI, static_path: StaticPathResolver) -> None:
    no_store = {"cache-control": "no-store"}

    @app.get("/{full_path:path}")
    def static_bundle(full_path: str) -> FileResponse:
        path = static_path(full_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="static bundle not found")
        return FileResponse(path, headers=no_store)


def build_dev_app(augur_config: Config, *, api_only: bool = False, price_clients: PriceClientsProvider) -> FastAPI:
    """The combined dev app (API routes plus, unless `api_only`, the static SPA bundle).

    `price_clients` is the live price source map for `/api/calibration/run`."""
    app = create_app_from_augur_config(augur_config, price_clients=price_clients)
    if not api_only:
        _mount_static_bundle(app, _fixture_bundle_static_path())
    return app


def main(argv: list[str] | None = None) -> int:
    args = build_configured_server_arg_parser(
        description="Serve the Augur public fixture dev app.",
        api_only_help="Serve only API routes; skip the dev static bundle.",
    ).parse_args(argv)
    config_path = Path(args.config).resolve() if args.config else resolve_augur_config_path()
    augur_config = load_augur_config(config_path)
    app = build_dev_app(augur_config, api_only=args.api_only, price_clients=default_price_clients())
    return run_app(app=app, augur_config=augur_config, host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
