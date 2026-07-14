"""Mount Haku's FastMCP app at its exact public resource URL."""

from __future__ import annotations

from typing import override

from fastmcp.server.auth.auth import AccessToken, AuthProvider
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import RedirectResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

_MCP_HTTP_METHODS = ["GET", "POST", "DELETE"]


class MountPrefixResourceAuthProvider(AuthProvider):
    """Treat an outer ASGI mount prefix as the protected MCP resource."""

    def __init__(self, delegate: AuthProvider) -> None:
        super().__init__(base_url=delegate.base_url, required_scopes=delegate.required_scopes)
        self._delegate = delegate

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self._delegate.verify_token(token)

    @override
    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        # FastMCP normally appends its inner route to base_url. Here base_url is
        # already the public MCP URL; the inner root route is only an ASGI detail.
        return self._delegate.get_routes(mcp_path=None)

    @override
    def get_well_known_routes(self, mcp_path: str | None = None) -> list[Route]:
        return self._delegate.get_well_known_routes(mcp_path=None)

    @override
    def get_middleware(self) -> list[Middleware]:
        return self._delegate.get_middleware()

    @override
    def _get_resource_url(self, path: str | None = None) -> AnyHttpUrl | None:
        return self.base_url


class _InnerRootApp:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app({**scope, "path": "/", "raw_path": b"/"}, receive, send)


class _CanonicalPathRedirect:
    def __init__(self, path: str) -> None:
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await RedirectResponse(self._path, status_code=307)(scope, receive, send)


def mount_mcp_app(app: Starlette, *, path: str, mcp_app: ASGIApp) -> None:
    """Mount a root-routed MCP app without redirecting its public mount point."""

    app.router.routes.extend(
        [
            Route(path, endpoint=_InnerRootApp(mcp_app), methods=_MCP_HTTP_METHODS),
            Route(f"{path}/", endpoint=_CanonicalPathRedirect(path), methods=_MCP_HTTP_METHODS),
        ]
    )
    app.mount(path, mcp_app)
