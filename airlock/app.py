"""Airlock Starlette application.

/healthz          — liveness probe (no auth)
/auth/config      — OIDC configuration for the SPA (no auth)
/mcp              — MCP endpoint (DualVerifierOIDCProxy handles auth, agents only)
/api              — REST API (operator JWT auth, decide scope)
/static/frontend  — bundled Svelte SPA assets
/                 — SPA shell
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from airlock.config import Settings
from airlock.coordinator import ActionCoordinator
from airlock.oidc_auth import DualVerifierOIDCProxy
from airlock.operator_api import create_operator_api
from airlock.predicates import load_predicate
from airlock.proxy_server import AirlockServer

logger = logging.getLogger(__name__)

_FRONTEND_DIST_DIR = Path(__file__).parent / "frontend" / "dist"


def _build_auth(settings: Settings) -> DualVerifierOIDCProxy:
    """Build the OIDCProxy auth provider.

    OIDCProxy handles DCR and OAuth flows (for Claude.ai web) while also accepting
    direct Authentik JWTs (for the OpenClaw auth proxy sidecar).
    """
    config_url = f"{settings.oidc_upstream_issuer.rstrip('/')}/.well-known/openid-configuration"
    logger.info("Using DualVerifierOIDCProxy (DCR-capable) with upstream %s", settings.oidc_upstream_issuer)
    return DualVerifierOIDCProxy(
        config_url=config_url,
        client_id=settings.oidc_upstream_client_id,
        client_secret=settings.oidc_client_secret,
        base_url=f"{settings.public_base_url}/mcp",
        issuer_url=settings.public_base_url,
        require_authorization_consent=False,
    )


class _MCPPathNorm:
    """Normalize POST/DELETE /mcp (no trailing slash) to /mcp/ before routing.

    Starlette's Mount("/mcp") returns PARTIAL for the exact path /mcp (no trailing
    slash) — the captured path group is empty, which is falsy. When the SPA catch-all
    Route("/{rest:path}") also partially matches /mcp with a non-GET method, it can
    win the routing race and return 405. Rewriting /mcp → /mcp/ ensures the Mount
    sees a FULL match and the MCP sub-app handles the request.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp" and scope.get("method") in {"POST", "DELETE"}:
            scope = {**scope, "path": "/mcp/"}
        await self.app(scope, receive, send)


def create_app(settings: Settings, *, include_static: bool = True) -> Any:
    """Build the Starlette app serving UI and MCP on a single port."""

    auth = _build_auth(settings)
    predicate = load_predicate(settings.predicate_path)
    coordinator = ActionCoordinator(db_path=settings.db_path)
    gate = AirlockServer(
        backends=settings.backends,
        predicate=predicate,
        public_base_url=settings.public_base_url,
        default_wait_mode=settings.default_wait_mode,
        coordinator=coordinator,
        auth=auth,
    )
    mcp_app = gate.http_app(path="/")
    operator_app = create_operator_api(coordinator=coordinator, oidc_issuer=settings.oidc_issuer)

    auth_config_response = JSONResponse(
        {
            "authority": settings.oidc_issuer,
            "client_id": settings.oidc_client_id,
            "redirect_uri": f"{settings.public_base_url}/auth/callback",
        }
    )

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def auth_config(request: Request) -> JSONResponse:
        return auth_config_response

    routes: list = [
        Route("/healthz", endpoint=healthz),
        Route("/auth/config", endpoint=auth_config),
        Mount("/mcp", app=mcp_app),
        Mount("/api", app=operator_app),
    ]

    if include_static:
        _html = (_FRONTEND_DIST_DIR / "index.html").read_text()

        async def index(request: Request) -> HTMLResponse:
            return HTMLResponse(_html)

        routes += [
            Mount("/static/frontend", StaticFiles(directory=str(_FRONTEND_DIST_DIR))),
            Route("/{rest:path}", endpoint=index),
        ]

    # Combined lifespan: coordinator storage init/close wraps MCP server lifespan.
    mcp_lifespan = mcp_app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(app: Starlette) -> AsyncGenerator[None]:
        await coordinator.initialize()
        try:
            async with mcp_lifespan(app):
                yield
        finally:
            await coordinator.close()

    return _MCPPathNorm(Starlette(routes=routes, lifespan=_lifespan))


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
