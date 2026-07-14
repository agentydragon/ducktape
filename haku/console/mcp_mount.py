"""Mount Haku's FastMCP app at its exact public resource URL."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

_MCP_HTTP_METHODS = ["GET", "POST", "DELETE"]


class _CanonicalPathRedirect:
    def __init__(self, path: str) -> None:
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await RedirectResponse(self._path, status_code=307)(scope, receive, send)


def mount_mcp_app(app: Starlette, *, path: str, mcp_app: ASGIApp) -> None:
    """Mount an MCP app whose transport route equals its public mount path."""

    app.router.routes.extend(
        [
            Route(path, endpoint=mcp_app, methods=_MCP_HTTP_METHODS),
            Route(f"{path}/", endpoint=_CanonicalPathRedirect(path), methods=_MCP_HTTP_METHODS),
            Route(f"{path}{path}", endpoint=_CanonicalPathRedirect(path), methods=_MCP_HTTP_METHODS),
        ]
    )
    app.mount(path, mcp_app)
