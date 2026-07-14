"""HTTP entrypoint for the generic Authentik-backed MCP OAuth facade."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from prometheus_client import start_http_server
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from mcp_infra.authentik_auth.auth import build_authentik_auth
from mcp_infra.oauth_facade.config import FacadeSettings
from mcp_infra.oauth_facade.proxy import build_proxy_server
from mcp_infra.oauth_facade.upstream_probe import ProbeState, run_probe_loop
from mcp_infra.persistence import OAuthClientStorage, build_client_storage
from mcp_infra.static_bearer import StaticBearerGuard
from mcp_infra.tool_filter import ToolFilterMiddleware

logger = logging.getLogger(__name__)


def build_server(
    settings: FacadeSettings, *, auth_provider: Any | None = None
) -> tuple[Any, OAuthClientStorage | None]:
    """Build the facade FastMCP server. `auth_provider` is injectable for tests.

    Returns `(server, client_storage)` so the caller can pre-warm the storage
    in the ASGI lifespan — without this the GLIDE client's first connect
    (~400ms cold) can exceed the per-request timeout on the very first OAuth
    callback, surfacing as `glide_shared.exceptions.TimeoutError: timed out`.
    """
    if settings.client_auth is not None:
        # Cluster-internal facade: the static bearer (enforced by StaticBearerGuard
        # in create_app) replaces the Authentik OAuth gate, so there is no OIDC
        # state to persist.
        server = build_proxy_server(settings)
        client_storage = None
    else:
        assert settings.auth is not None  # guaranteed by FacadeSettings validator
        client_storage = build_client_storage(settings.persistence)
        auth = (
            auth_provider
            if auth_provider is not None
            else build_authentik_auth(settings.auth, client_storage=client_storage)
        )
        server = build_proxy_server(settings, auth=auth)
    if settings.tools is not None:
        server.add_middleware(ToolFilterMiddleware(settings.tools))
    if settings.logging.mcp_messages:
        server.add_middleware(
            StructuredLoggingMiddleware(
                log_level=logging.getLevelNamesMapping()[settings.logging.mcp_message_level],
                include_payloads=settings.logging.mcp_payloads,
                include_payload_length=settings.logging.mcp_payload_length,
                methods=settings.logging.mcp_methods,
            )
        )
    return server, client_storage


def create_app(settings: FacadeSettings, *, auth_provider: Any | None = None) -> Starlette:
    server, client_storage = build_server(settings, auth_provider=auth_provider)
    mcp_app = server.http_app(path="/mcp")
    probe_state = ProbeState(
        facade_name=settings.facade_name, max_staleness_seconds=settings.probe_max_staleness_seconds
    )

    async def healthz(request: Request) -> JSONResponse:
        """Process liveness only — does not reflect upstream tool availability."""
        return JSONResponse({"ok": True})

    async def readyz(request: Request) -> JSONResponse:
        """Ready only when the upstream is actually serving tools (see ProbeState)."""
        ready = probe_state.ready()
        return JSONResponse(
            {"ready": ready, "tools": probe_state.last_success_tools}, status_code=200 if ready else 503
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if client_storage is not None:
            await client_storage.setup()
            logger.info("client_storage pre-warmed (no lazy first-request init)")
        # Metrics on a dedicated cluster-internal port, off the public HTTPRoute.
        start_http_server(settings.metrics_port)
        logger.info("prometheus metrics on :%d", settings.metrics_port)
        probe_task = asyncio.create_task(run_probe_loop(settings, probe_state))
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await probe_task

    mounted_app: ASGIApp = mcp_app
    if settings.client_auth is not None:
        mounted_app = StaticBearerGuard(mcp_app, token=settings.client_auth.static_bearer)
    return Starlette(
        routes=[Route("/healthz", healthz), Route("/readyz", readyz), Mount("/", app=mounted_app)], lifespan=lifespan
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = FacadeSettings()
    app = create_app(settings)
    logger.info("%s listening on %s:%d", settings.facade_name, settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
