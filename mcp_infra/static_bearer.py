"""ASGI guard requiring a fixed bearer token, for cluster-internal MCP endpoints."""

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class StaticBearerGuard:
    """ASGI guard requiring a fixed `Authorization: Bearer <token>` on the wrapped app.

    For cluster-internal MCP endpoints whose only access control is a shared secret
    (plus the network boundary). Wrap only the protected mount and register
    liveness/readiness routes earlier, so k8s probes reach them without the token.
    """

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            presented = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(presented, self._expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self._app(scope, receive, send)
