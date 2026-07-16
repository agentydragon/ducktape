"""Airlock OAuth credential-broker application.

The service exposes a browser UI and authenticated REST status endpoints for
upstream OAuth providers. It deliberately has no MCP or tool-approval surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastmcp.server.auth.auth import AccessToken, AuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.responses import HTMLResponse

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

logger = logging.getLogger(__name__)

_FRONTEND_DIST_DIR = Path(__file__).parent / "frontend" / "dist"
_NS_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def _detect_namespace() -> str:
    """Read the in-cluster namespace, falling back to ``airlock``."""
    if _NS_PATH.exists():
        return _NS_PATH.read_text().strip()
    return "airlock"


def _require_authenticated(auth: AuthProvider) -> Callable[[Request], Awaitable[AccessToken]]:
    """Build a FastAPI dependency requiring a valid Bearer JWT."""

    async def dependency(request: Request) -> AccessToken:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        access = await auth.verify_token(token)
        if access is None:
            raise HTTPException(
                status_code=401, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
            )
        return access

    return dependency


def create_app(settings: Settings, *, auth: AuthProvider, include_static: bool = True) -> FastAPI:
    """Build the FastAPI app serving the OAuth broker UI and API."""
    require_authenticated = Depends(_require_authenticated(auth))
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

        app.include_router(create_oauth_router(oauth_providers, oauth_k8s_store, oauth_target_ns))

        if include_static:
            html = (_FRONTEND_DIST_DIR / "index.html").read_text()
            app.mount("/static/frontend", StaticFiles(directory=str(_FRONTEND_DIST_DIR)))

            @app.get("/")
            @app.get("/auth/callback")
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

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/auth/config")
    async def auth_config() -> dict[str, str]:
        return {
            "authority": settings.oidc_issuer,
            "client_id": settings.oidc_client_id,
            "redirect_uri": f"{settings.public_base_url}/auth/callback",
        }

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


def _build_auth(settings: Settings) -> AuthProvider:
    """Discover the Authentik JWKS endpoint and build a JWT verifier."""
    discovery_url = f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
    discovery = httpx.get(discovery_url, timeout=10.0).raise_for_status().json()
    return JWTVerifier(jwks_uri=discovery["jwks_uri"])


async def _serve() -> None:
    settings = Settings.load()
    app = create_app(settings, auth=_build_auth(settings))
    logger.info("serving on %s:%d", settings.host, settings.port)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info"))
    await server.serve()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
