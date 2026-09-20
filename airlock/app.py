"""Airlock OAuth credential-broker application.

The service exposes a browser UI and authenticated REST status endpoints for
upstream OAuth providers. It deliberately has no MCP or tool-approval surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import HTMLResponse

from airlock.auth import build_oauth, create_auth_router, require_operator_session, session_cookie_name
from airlock.config import Settings, build_oauth_providers
from airlock.deployment import build_deployment_info
from airlock.models import (
    ConnectedOAuthStatus,
    DeploymentInfo,
    DisconnectedOAuthStatus,
    ExpiredOAuthStatus,
    OAuthConnectionStatus,
    OAuthProviderStatus,
)
from airlock.oauth.k8s_client import K8sTokenStore
from airlock.oauth.provider import GenericOAuth2Provider
from airlock.oauth.refresh import token_refresh_loop
from airlock.oauth.routes import create_oauth_router

# SessionMiddleware imports itsdangerous lazily; keep it a direct runtime dependency.
# gazelle:include_dep @pypi//itsdangerous

logger = logging.getLogger(__name__)

_FRONTEND_DIST_DIR = Path(__file__).parent / "frontend" / "dist"
_NS_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def _detect_namespace() -> str:
    """Read the in-cluster namespace, falling back to ``airlock``."""
    if _NS_PATH.exists():
        return _NS_PATH.read_text().strip()
    return "airlock"


def create_app(settings: Settings, *, include_static: bool = True) -> FastAPI:
    """Build the FastAPI app serving the OAuth broker UI and API."""
    require_authenticated = Depends(require_operator_session)
    oauth_providers: dict[str, GenericOAuth2Provider] = {}
    oauth_k8s_store: K8sTokenStore | None = None
    oauth_target_ns = ""
    oauth_refresh_errors: dict[str, str] = {}

    @contextlib.asynccontextmanager
    async def app_lifespan(app: FastAPI):
        nonlocal oauth_providers, oauth_k8s_store, oauth_target_ns
        oauth_providers = build_oauth_providers(settings.oauth, f"{settings.public_base_url}/oauth/callback")
        oauth_k8s_store = await K8sTokenStore.from_incluster(managed_by=settings.oauth.managed_by)
        oauth_target_ns = settings.oauth.target_namespace or _detect_namespace()

        app.include_router(create_oauth_router(oauth_providers, oauth_k8s_store, oauth_target_ns, settings))

        if include_static:
            html = (_FRONTEND_DIST_DIR / "index.html").read_text()
            app.mount("/static/frontend", StaticFiles(directory=str(_FRONTEND_DIST_DIR)))

            @app.get("/")
            async def index() -> HTMLResponse:
                return HTMLResponse(html)

        task = asyncio.create_task(
            token_refresh_loop(oauth_providers, oauth_k8s_store, oauth_target_ns, refresh_errors=oauth_refresh_errors)
        )
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Airlock OAuth", docs_url=None, redoc_url=None, lifespan=app_lifespan)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.oidc_session_secret.get_secret_value(),
        session_cookie=session_cookie_name(settings),
        max_age=settings.session_seconds,
        same_site="lax",
        https_only=settings.public_base_url.startswith("https://"),
        path="/",
    )
    app.state.oauth = build_oauth(settings)
    app.include_router(create_auth_router(settings))

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    deployment_info = build_deployment_info()

    @app.get("/api/info", dependencies=[require_authenticated])
    async def get_info() -> DeploymentInfo:
        return deployment_info

    @app.get("/api/oauth/providers", dependencies=[require_authenticated])
    async def list_oauth_providers() -> list[OAuthProviderStatus]:
        assert oauth_k8s_store is not None
        result: list[OAuthProviderStatus] = []
        for name, provider in oauth_providers.items():
            token = await oauth_k8s_store.read_token(provider.config.refresh_secret.name, oauth_target_ns)
            status: OAuthConnectionStatus
            if token is None:
                status = DisconnectedOAuthStatus()
            elif token.expires_at <= datetime.now(UTC):
                status = ExpiredOAuthStatus(
                    expires_at=token.expires_at, scope=token.scope, last_refresh_error=oauth_refresh_errors.get(name)
                )
            else:
                status = ConnectedOAuthStatus(expires_at=token.expires_at, scope=token.scope)
            result.append(
                OAuthProviderStatus(
                    name=name,
                    display_name=provider.config.display_name,
                    provider_type="oauth2",
                    requested_scopes=list(provider.config.scopes),
                    status=status,
                )
            )
        return result

    return app


async def _serve() -> None:
    settings = Settings.load()
    app = create_app(settings)
    logger.info("serving on %s:%d", settings.host, settings.port)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info"))
    await server.serve()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
