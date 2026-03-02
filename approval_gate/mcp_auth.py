"""FastMCP auth configuration for the approval gate.

All tokens (operator JWT via Authentik proxy outpost, agent JWT via
client_credentials) are verified as JWTs against the same JWKS endpoint.
Scopes in the JWT determine capabilities:

  propose — agent: wrapped backend tools, withdraw_action
  decide  — operator: approve_action, reject_action
  read    — both: list_actions, resource reads

Operator tokens arrive via the ``x-authentik-jwt`` header (set by Authentik
proxy outpost). ``AuthentikHeaderNormalizer`` copies them into a standard
``Authorization: Bearer`` header so FastMCP's ``JWTVerifier`` can process
them identically to agent tokens.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

PROPOSE_SCOPE = "propose"
DECIDE_SCOPE = "decide"
READ_SCOPE = "read"


class AuthentikHeaderNormalizer:
    """ASGI middleware: copies ``x-authentik-jwt`` to ``Authorization: Bearer``.

    Must be mounted *outside* the FastMCP app so the header is visible to
    FastMCP's built-in ``BearerAuthBackend`` middleware.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope["headers"])
            if b"x-authentik-jwt" in headers and b"authorization" not in headers:
                jwt = headers[b"x-authentik-jwt"]
                scope = dict(scope)
                scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"x-authentik-jwt"] + [
                    (b"authorization", b"Bearer " + jwt)
                ]
        await self.app(scope, receive, send)
