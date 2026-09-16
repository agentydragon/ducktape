"""Mounting Connect RPC services on the app's own origin, behind the app's own caller check.

The browser reaches these at the same host, port and cookie as the REST surface, so nothing about
authentication is special to them: `Guarded` runs the same `require_caller` the routes depend on,
because a mount is not a route and would otherwise inherit nothing.
"""

from __future__ import annotations

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from x.agentplane.app.identity import CallerCheck

# One mount point for every Connect service: the fully qualified service name is the rest of the
# path, which connecpy routes on, so a client is configured with a base URL rather than per-service
# paths.
RPC_PREFIX = "/rpc"


class Guarded:
    """A mounted Connect application behind the caller check every REST route depends on.

    Applying the same check is the point: a second authorization implementation beside the first is
    the risk that a separate RPC server would have carried. It is passed in rather than imported
    because a mount is not a route, so `dependency_overrides` cannot reach it and whoever builds
    the app has to hand both surfaces the same one. Its `HTTPException` reaches FastAPI's handler
    from here exactly as it does from a route, because a mount is dispatched below the exception
    middleware.
    """

    def __init__(self, app: ASGIApp, *, caller: CallerCheck) -> None:
        self._app = app
        self._caller = caller

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._caller(Request(scope, receive))
        await self._app(scope, receive, send)
