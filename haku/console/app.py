"""FastAPI app for the Haku console JSON API.

The console is the trusted outer shell: it frames Haku's own UI (haku-state's ``ui/``)
full-page as a sandboxed cross-origin iframe and owns the one privileged surface — the
**capability tier** (``haku.console.capabilities``), which uses console-only secrets and
acts on the world (launching the routine); it is same-origin gated and audited (see
``haku/docs/security.md`` → enforcement inventory, "Console privileged-action tier"). ``app.py``
wires that router and serves the config endpoint. It can also mount the built SPA when
``static_dir`` is explicitly configured for a direct local/dev fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import Lifespan

from haku.console import capabilities
from haku.console.database_migrate import main as migration_main, verify_schema
from haku.console.deployment import DeploymentInfo, build_deployment_info
from haku.console.deps import SettingsDep
from haku.console.identity import agent_routes, operator_auth, operator_login_flow
from haku.console.identity.authorization import PostgresAgentAuthority
from haku.console.identity.operator_identity import OperatorIdentityTrust
from haku.console.identity.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.models import ConfigResponse
from haku.console.settings import Settings

# SessionMiddleware signs cookies with itsdangerous, imported inside starlette;
# gazelle cannot see the dependency.
# gazelle:include_dep @pypi//itsdangerous

APP_SHELL_CACHE_CONTROL = "no-store"
IMMUTABLE_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_STORE_CACHE_CONTROL = "no-store"
REFERRER_POLICY = "no-referrer"
# The shell (top-level) may read geolocation for the `requestGeolocation` Agent UI bridge action, and
# capture the screen for the `requestScreenshot` Agent UI bridge action; `(self)` scopes both to the
# shell origin so neither is ever delegated to the framed haku-ui origin — the frame stays
# unable to read location or capture the screen on its own (docs/containment.md).
PERMISSIONS_POLICY = "geolocation=(self), display-capture=(self)"


class _ConsoleStaticFiles(StaticFiles):
    """Direct dev static serving that never conditionally reuses the SPA shell.

    Bazel-normalized mtimes and same-sized shells can otherwise produce a false 304 after the
    embedded fingerprint changes. Production nginx has the same contract via ``etag off`` and
    ``if_modified_since off``.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: MutableMapping[str, Any],
        status_code: int = 200,
    ) -> Response:
        path = Path(full_path)
        if path.name == "index.html":
            return Response(
                content=path.read_bytes(),
                headers={"Cache-Control": APP_SHELL_CACHE_CONTROL},
                media_type="text/html",
                status_code=status_code,
            )
        return super().file_response(full_path, stat_result, scope, status_code=status_code)


def _cache_control_for_path(path: str, status_code: int) -> str:
    if path.startswith("/_console/assets/") and status_code in {200, 206, 304}:
        return IMMUTABLE_ASSET_CACHE_CONTROL
    # The app is authoritative for the cache policy of every backend surface it serves — nginx no
    # longer sets Cache-Control on proxied responses (haku/console/default.conf.template), so these
    # prefixes must be listed here, not there. Keep in sync with the proxied `location`s.
    if path.startswith(("/api/", "/auth/", "/metrics")) or path == "/healthz":
        return NO_STORE_CACHE_CONTROL
    return APP_SHELL_CACHE_CONTROL


def _operator_identity_trust(settings: Settings) -> OperatorIdentityTrust:
    return OperatorIdentityTrust(
        trust_domain=settings.operator_identity.trust_domain, trusted_issuers=frozenset({settings.operator_oidc.issuer})
    )


def build_app(*, lifespan: Lifespan[FastAPI] | None) -> FastAPI:
    """The production route graph on a bare ``FastAPI``.

    Every handler and guard reaches settings, stores and the OAuth registry through ``app.state`` at
    request time, so the routes register without any runtime resource; ``create_app`` attaches
    those plus the middleware, and ``export_schema`` reads ``app.openapi()`` off this alone.
    """
    app = FastAPI(title="Haku console", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok"})

    # Prometheus scrape target. Deliberately absent from default.conf.template's proxied
    # `location`s: nginx serves the public origin, so an unproxied /metrics stays reachable only
    # on the pod's own API port, which is what the ServiceMonitor's `metrics` port targets.
    # A route, not app.mount(): a Mount only matches paths *under* its prefix, so bare /metrics
    # would fall through to the SPA catch-all and return the app shell.
    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # The browser API is operator-only and recognizes the Operator session.
    operator_only = [Depends(operator_auth.require_operator), Depends(operator_auth.require_operator_mutation_origin)]
    app.include_router(capabilities.router, dependencies=operator_only)
    app.include_router(agent_routes.router, dependencies=operator_only)

    @app.get("/api/deployment", dependencies=operator_only)
    async def deployment(settings: SettingsDep) -> DeploymentInfo:
        # The static shell is an independent Deployment. Its Flux-selected tag is
        # read from a projected ConfigMap on every request so a frontend-only roll
        # does not need to restart this API pod merely to update Settings metadata.
        return build_deployment_info(
            image_tag=settings.image_tag,
            static_image_tag=settings.static_image_tag,
            static_image_tag_file=settings.static_image_tag_file,
        )

    @app.get("/api/config", dependencies=operator_only)
    async def config(settings: SettingsDep) -> ConfigResponse:
        """Static config for the SPA."""
        launch = settings.launch_routine
        return ConfigResponse(launch_routine_url=launch.page_url if launch else None, haku_ui_url=settings.haku_ui_url)

    app.include_router(operator_auth.router)
    return app


def create_app(settings: Settings) -> FastAPI:
    # Deploy-time Console config file (non-secret): Agent access profiles.
    # Postgres is required for operator identity and login-flow state. Construction is lazy (no
    # connect); migrations run once at startup (app.main / the test fixture), not here.
    # One engine/sessionmaker for the whole console, injected into every SQLAlchemy store, so the
    # process holds a single connection pool rather than one per store.
    # Configuration accepts a driver-neutral URI and existing explicit PostgreSQL driver schemes.
    database_url = make_url(settings.database_url.get_secret_value()).set(drivername="postgresql+asyncpg")
    # SQLAlchemy loads asyncpg at runtime from the URL selected here.
    # gazelle:include_dep @pypi//asyncpg
    db_engine = create_async_engine(database_url, pool_pre_ping=True)
    db_sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    operator_identity_store = PostgresOperatorIdentityStore(db_sessions, _operator_identity_trust(settings))
    operator_login_flows = operator_login_flow.PostgresOperatorLoginFlowStore(db_sessions)
    agent_authority = PostgresAgentAuthority(
        db_sessions,
        operator_identity_store=operator_identity_store,
        access_profiles=tuple(profile.id for profile in settings.access_profiles),
        default_access_profile_id=settings.default_access_profile_id,
    )

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            # Register before startup work so partial startup still disposes the pool.
            stack.push_async_callback(db_engine.dispose)
            await stack.enter_async_context(agent_authority.expiry_maintenance())
            yield

    app = build_app(lifespan=_lifespan)
    # The routers and guards read settings off app.state (see haku.console.deps).
    app.state.settings = settings
    # Expose the shared database resources to internal dependencies and diagnostics; every store
    # above uses these same objects rather than creating a second pool.
    app.state.db_engine = db_engine
    app.state.db_sessions = db_sessions
    app.state.agent_enrollment_service = agent_authority
    app.state.operator_identity_store = operator_identity_store
    app.state.operator_login_flows = operator_login_flows
    # Operator browser auth is mandatory; the /auth router drives the registry per request.
    app.state.operator_oauth = operator_auth.build_oauth(settings.operator_oidc, login_flows=operator_login_flows)

    # Content-Security-Policy: let the console frame Haku's own UI origin (the sandboxed
    # cross-origin iframe) and Authentik's origin for the SSO redirect, and forbid the
    # console itself from being framed. Only frame-* is set, so the SPA's own scripts/styles
    # are unaffected. See haku/console/docs/containment.md.
    csp = f"frame-src 'self' {settings.haku_ui_url} {settings.auth_origin}; frame-ancestors 'none'"

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        # The operator-login failure response carries its own stricter, nonce-bound policy.
        # Preserve it; this default governs the SPA/API surfaces.
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers["Cache-Control"] = _cache_control_for_path(request.url.path, response.status_code)
        response.headers.setdefault("Referrer-Policy", REFERRER_POLICY)
        response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        return response

    # SessionMiddleware establishes request.session, which the router guards read; https_only
    # follows the canonical public origin.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.operator_oidc.session_secret.get_secret_value(),
        https_only=settings.public_base_url.startswith("https://"),
        same_site="lax",
        max_age=operator_auth.OPERATOR_SESSION_MAX_AGE_SECONDS,
    )

    # Optional direct local/dev fallback. Production serves the SPA from the
    # haku-console-static nginx image and leaves static_dir unset on this process.
    # Mounted last so the API routes above take precedence.
    if settings.static_dir is not None and settings.static_dir.is_dir():
        index_file = settings.static_dir / "index.html"

        # The production image exposes fingerprinted browser assets under the reserved console
        # namespace even though they remain in dist/assets on disk. Mirror that mapping here;
        # mounting it before the SPA fallback also keeps a missing asset a real 404.
        assets_dir = settings.static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/_console/assets", _ConsoleStaticFiles(directory=assets_dir), name="console-assets")

        # Every other browser path is a client-side route: /_console/* selects trusted console
        # pages, while all other paths are mirrored Haku UI routes.
        @app.get("/{spa_path:path}")
        async def _spa_route(spa_path: str) -> Response:
            return Response(content=index_file.read_bytes(), media_type="text/html")

    return app


async def _serve(app: FastAPI) -> None:
    """Serve the network app."""
    # Host/port are intentionally fixed process topology, not deploy settings. Ordinary Console
    # settings use the collision-resistant HAKU_CONSOLE__ nested prefix.
    #
    # `timeout_graceful_shutdown` bounds how long uvicorn waits on SIGTERM for in-flight requests
    # to drain before cancelling handlers and running the lifespan shutdown. Keep it below the
    # deployment's terminationGracePeriodSeconds.
    network = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info", timeout_graceful_shutdown=10)
    )
    await network.serve()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings()
    # DDL belongs to the image-coupled release Job. Before binding a port, prove this image can
    # read the already-migrated schema so an incompatible rollout never becomes Ready.
    verify_schema(settings.database_url.get_secret_value())
    app = create_app(settings)
    asyncio.run(_serve(app))


def run_command(argv: list[str]) -> None:
    """Dispatch the image's two process modes without constructing application settings for migrations."""
    if argv == ["migrate"]:
        migration_main()
        return
    if argv:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} [migrate]")
    main()


if __name__ == "__main__":
    run_command(sys.argv[1:])
