"""Bearer-protected server exposing haku-console's Gmail/Calendar tools for agentplane.

Reuses haku-console's tool definitions (`haku.console.tools.gmail`, `.google_calendar`) as-is,
against a separate, agentplane-owned Google credential -- an Airlock-minted access token read
once at startup, not haku-console's per-Operator OAuth connection. Reloader restarts this pod
whenever the mounted token Secret rotates (the same mechanism ssh-mcp/ha-mcp already rely on for
their own caller-facing bearer rotation), which keeps the held token within Airlock's refresh
margin without any custom per-call refresh logic here.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from haku.console.tools import gmail as gmail_tools, google_calendar as calendar_tools
from mcp_infra.static_bearer import StaticBearerGuard

logger = logging.getLogger(__name__)


def create_app(*, google_access_token: str, bearer: str) -> Starlette:
    gmail_app = gmail_tools.build_mcp(gmail_tools.build_gmail_client_from_token(google_access_token)).http_app(
        path="/mcp", stateless_http=True
    )
    calendar_app = calendar_tools.build_mcp(
        calendar_tools.build_calendar_client_from_token(google_access_token)
    ).http_app(path="/mcp", stateless_http=True)

    async def healthz(request: Request) -> JSONResponse:
        del request
        return JSONResponse({"ok": True})

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        del app
        async with gmail_app.lifespan(gmail_app), calendar_app.lifespan(calendar_app):
            yield

    protected_gmail: ASGIApp = StaticBearerGuard(gmail_app, token=bearer)
    protected_calendar: ASGIApp = StaticBearerGuard(calendar_app, token=bearer)
    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Mount("/gmail", app=protected_gmail),
            Mount("/calendar", app=protected_calendar),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr)
    google_access_token = Path(os.environ["GOOGLE_MCP_TOKEN_FILE"]).read_text().strip()
    app = create_app(google_access_token=google_access_token, bearer=os.environ["GOOGLE_MCP_BEARER_TOKEN"])
    uvicorn.run(
        app, host=os.environ.get("GOOGLE_MCP_HOST", "0.0.0.0"), port=int(os.environ.get("GOOGLE_MCP_PORT", "8080"))
    )


if __name__ == "__main__":
    main()
