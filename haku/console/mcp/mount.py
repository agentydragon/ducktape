"""Mount Haku's FastMCP app at its exact public resource URL."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

_MCP_HTTP_METHODS = ["GET", "POST", "DELETE"]

# CLEANUP(added 2026-09-07): mcp==1.29.0's StreamableHTTPSessionManager (vendored into
# fastmcp==3.4.4) stores its task group in one shared instance attribute and tears it down
# permanently the first time any per-request task raises an uncaught CancelledError (e.g. a client
# disconnecting mid-request) — a case its own `except Exception` does not catch, since
# CancelledError is a BaseException (modelcontextprotocol/python-sdk#820, #1219). Every subsequent
# request to that replica's /mcp then 500s until the process restarts, and a bare TCP
# liveness/readiness probe never notices. FastMCP already detects the wedge and re-raises it with
# this distinctive message (fastmcp/server/http.py's StreamableHTTPASGIApp.__call__); watching for
# it here is far less fragile than reaching into the SDK's private task-group state. Delete this,
# and `McpSessionManagerHealth`'s use in app.py's `/healthz` and the cluster probes, once the
# pinned SDK stops exhibiting the failure (mcp's 1.x line is done; the fix landed in the breaking
# v2 rework, which is its own migration).
_SESSION_MANAGER_DEAD_MESSAGE_PREFIX = "FastMCP's StreamableHTTPSessionManager task group was not initialized."


class McpSessionManagerHealth:
    """Tracks whether FastMCP's mounted StreamableHTTP session manager has wedged. See the
    CLEANUP note above `_SESSION_MANAGER_DEAD_MESSAGE_PREFIX` for why this exists."""

    def __init__(self) -> None:
        self.alive = True

    def observe(self, exc: BaseException) -> None:
        if isinstance(exc, RuntimeError) and str(exc).startswith(_SESSION_MANAGER_DEAD_MESSAGE_PREFIX):
            self.alive = False


class _ObserveSessionManagerHealth:
    """Forward every call to `app` unchanged, only watching failures for the wedge above."""

    def __init__(self, app: ASGIApp, health: McpSessionManagerHealth) -> None:
        self._app = app
        self._health = health

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self._app(scope, receive, send)
        except RuntimeError as exc:
            self._health.observe(exc)
            raise


class _CanonicalPathRedirect:
    def __init__(self, path: str) -> None:
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await RedirectResponse(self._path, status_code=307)(scope, receive, send)


def mount_mcp_app(app: Starlette, *, path: str, mcp_app: ASGIApp, health: McpSessionManagerHealth) -> None:
    """Mount an MCP app whose transport route equals its public mount path."""

    observed_app = _ObserveSessionManagerHealth(mcp_app, health)
    app.router.routes.extend(
        [
            Route(path, endpoint=observed_app, methods=_MCP_HTTP_METHODS),
            Route(f"{path}/", endpoint=_CanonicalPathRedirect(path), methods=_MCP_HTTP_METHODS),
            Route(f"{path}{path}", endpoint=_CanonicalPathRedirect(path), methods=_MCP_HTTP_METHODS),
        ]
    )
    app.mount(path, observed_app)
