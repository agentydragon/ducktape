"""FastMCP AuthProvider for the approval gate.

Handles two auth methods on a single /mcp endpoint:
  - x-authentik-jwt: <jwt>         → operator (scopes: ["operator", "reader"])
  - Authorization: Bearer <key>    → agent   (scopes: ["agent", "reader"])

For in-process (stdio/memory) Client connections, FastMCP bypasses all auth checks
entirely — tests connect directly to ApprovalGateServer without needing any credentials.
"""

from __future__ import annotations

import asyncio
import logging

import jwt as pyjwt
from fastmcp.server.auth import AccessToken, AuthProvider
from jwt import InvalidTokenError, PyJWKClient
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.authentication import AuthCredentials, AuthenticationBackend
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)

AGENT_SCOPE = "agent"
OPERATOR_SCOPE = "operator"
READER_SCOPE = "reader"


class ApprovalGateAuthProvider(AuthProvider):
    """Authenticates both Authentik JWT (operators) and bearer API keys (agents).

    Both roles include ``READER_SCOPE`` for resource reads and ``list_actions``.
    Agents carry ``Authorization: Bearer <AGENT_API_KEY>`` and additionally receive ``AGENT_SCOPE``,
    which gates wrapped backend tools and ``withdraw_action``.
    Operators carry ``x-authentik-jwt: <jwt>`` and additionally receive ``OPERATOR_SCOPE``,
    which gates approve_action / reject_action.
    """

    def __init__(self, *, agent_api_key: str, jwks_client: PyJWKClient) -> None:
        super().__init__(required_scopes=[])
        self._agent_api_key = agent_api_key
        self._jwks_client = jwks_client

    def get_middleware(self) -> list:
        """Override to install a dual-header auth backend instead of the standard Bearer-only one."""
        return [
            Middleware(AuthenticationMiddleware, backend=_DualHeaderAuthBackend(self)),
            Middleware(AuthContextMiddleware),
        ]

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a token string and return an AccessToken with appropriate scopes.

        Tokens are prefixed by the ``_DualHeaderAuthBackend`` to indicate their origin:
        - ``"jwt:<raw_jwt>"`` — operator Authentik JWT
        - bare string         — agent bearer key
        """
        if token.startswith("jwt:"):
            return await self._verify_operator_jwt(token[4:])
        return self._verify_agent_bearer(token)

    def _verify_agent_bearer(self, token: str) -> AccessToken | None:
        if token == self._agent_api_key:
            return AccessToken(token=token, client_id="agent", scopes=[AGENT_SCOPE, READER_SCOPE])
        return None

    async def _verify_operator_jwt(self, raw_jwt: str) -> AccessToken | None:
        try:
            signing_key = await asyncio.to_thread(self._jwks_client.get_signing_key_from_jwt, raw_jwt)
            pyjwt.decode(
                raw_jwt,
                signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                options={"verify_aud": False},
            )
        except InvalidTokenError as exc:
            logger.warning("operator JWT verification failed: %s", exc)
            return None
        except Exception as exc:
            logger.error("JWT verification error: %s", exc)
            return None
        # Any valid JWT is treated as an operator; group checks are left to the JWKS
        # issuer policy. If finer-grained enforcement is needed, add a groups check here.
        return AccessToken(token=raw_jwt, client_id="operator", scopes=[OPERATOR_SCOPE, READER_SCOPE])


class _DualHeaderAuthBackend(AuthenticationBackend):
    """Starlette auth backend that reads both ``x-authentik-jwt`` and ``Authorization: Bearer``."""

    def __init__(self, provider: ApprovalGateAuthProvider) -> None:
        self._provider = provider

    async def authenticate(self, conn: HTTPConnection):
        jwt_header = conn.headers.get("x-authentik-jwt")
        if jwt_header:
            token_str = f"jwt:{jwt_header}"
        else:
            auth = conn.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return None
            token_str = auth.removeprefix("Bearer ")

        access_token = await self._provider.verify_token(token_str)
        if access_token is None:
            return None
        return AuthCredentials(access_token.scopes), AuthenticatedUser(access_token)
