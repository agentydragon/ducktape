"""Approval Gate Starlette application.

/healthz          — liveness probe (no auth)
/mcp              — MCP endpoint (FastMCP handles auth via ApprovalGateAuthProvider)
/static/frontend  — bundled Svelte SPA assets
/                 — SPA shell
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import uvicorn
from jwt import PyJWKClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from approval_gate.config import Settings
from approval_gate.mcp_auth import ApprovalGateAuthProvider
from approval_gate.predicates import load_predicate
from approval_gate.proxy_server import ApprovalGateServer

logger = logging.getLogger(__name__)

_FRONTEND_DIST_DIR = Path(__file__).parent / "frontend" / "dist"


def create_app(settings: Settings, *, include_static: bool = True) -> Starlette:
    """Build the Starlette app serving UI and MCP on a single port."""
    predicate = load_predicate(settings.predicate_path)
    jwks_client = PyJWKClient(settings.operator_jwks_url)
    auth = ApprovalGateAuthProvider(agent_api_key=settings.agent_api_key, jwks_client=jwks_client)
    gate = ApprovalGateServer(
        backends=settings.backends,
        db_path=settings.db_path,
        predicate=predicate,
        public_base_url=settings.public_base_url,
        auth=auth,
    )
    mcp_app = gate.http_app(path="/")

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    routes: list = [Route("/healthz", endpoint=healthz), Mount("/mcp", app=mcp_app)]

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
