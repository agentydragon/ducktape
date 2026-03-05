"""Approval Gate Starlette application.

/healthz          — liveness probe (no auth)
/auth/config      — OIDC configuration for the SPA (no auth)
/mcp              — MCP endpoint (JWTVerifier handles auth via JWKS)
/static/frontend  — bundled Svelte SPA assets
/                 — SPA shell
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from approval_gate.config import Settings
from approval_gate.predicates import load_predicate
from approval_gate.proxy_server import ApprovalGateServer

logger = logging.getLogger(__name__)

_FRONTEND_DIST_DIR = Path(__file__).parent / "frontend" / "dist"


def _fetch_oidc_discovery(issuer: str) -> dict[str, Any]:
    """Fetch the OpenID Connect discovery document from the issuer."""
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def create_app(settings: Settings, *, include_static: bool = True) -> Starlette:
    """Build the Starlette app serving UI and MCP on a single port."""
    discovery = _fetch_oidc_discovery(settings.oidc_issuer)
    predicate = load_predicate(settings.predicate_path)
    gate = ApprovalGateServer(
        backends=settings.backends,
        db_path=settings.db_path,
        predicate=predicate,
        public_base_url=settings.public_base_url,
        default_wait_mode=settings.default_wait_mode,
        auth=JWTVerifier(jwks_uri=discovery["jwks_uri"]),
    )
    mcp_app = gate.http_app(path="/")

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
    ]

    if include_static:
        _html = (_FRONTEND_DIST_DIR / "index.html").read_text()

        async def index(request: Request) -> HTMLResponse:
            return HTMLResponse(_html)

        routes += [
            Mount("/static/frontend", StaticFiles(directory=str(_FRONTEND_DIST_DIR))),
            Route("/{rest:path}", endpoint=index),
        ]

    # Delegate lifespan to the FastMCP app — it manages the gate's startup/shutdown.
    return Starlette(routes=routes, lifespan=mcp_app.lifespan)


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
