"""Verify the MCP transport's bearer as a Sandbox workload or an external OAuth grant.

FastMCP runs this verifier on every transport request (`RequireAuthMiddleware` around `/mcp`) and
hands tools the resulting `CallerToken` through `CurrentAccessToken`; no identity rides on the
request. Deviation from FastMCP's stock bearer backend: exactly one Authorization header is
accepted, as on the workload HTTP routes, and a grant-authority outage answers with its own 503
instead of a false `invalid_token`.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, BearerAuthBackend
from starlette.authentication import AuthCredentials, AuthenticationError
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from x.agentplane.action_service.auth import workload_principal
from x.agentplane.action_service.models import ExternalGrantProvenance, Principal
from x.agentplane.action_service.oauth import ActionsOAuthProxy
from x.agentplane.sandbox_auth.principal import SandboxPrincipalRejectedError, SandboxPrincipalResolver


class CallerToken(AccessToken):
    """The verified identity FastMCP carries for one transport request."""

    principal: Principal
    external_grant: ExternalGrantProvenance | None


class CallerAuthorityUnavailableError(AuthenticationError):
    """The grant authority could not answer, so the bearer was neither accepted nor refused."""

    def __init__(self, response: HTTPException) -> None:
        super().__init__(response.detail)
        self.response = response


class _SoleBearerBackend(BearerAuthBackend):
    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, AuthenticatedUser] | None:
        if len(conn.headers.getlist("authorization")) != 1:
            return None
        try:
            authenticated: tuple[AuthCredentials, AuthenticatedUser] | None = await super().authenticate(conn)
        except HTTPException as error:
            raise CallerAuthorityUnavailableError(error) from error
        return authenticated


def _authority_unavailable(conn: HTTPConnection, error: AuthenticationError) -> Response:
    del conn
    if not isinstance(error, CallerAuthorityUnavailableError):
        raise error
    return JSONResponse(
        {"detail": error.response.detail}, status_code=error.response.status_code, headers=error.response.headers
    )


class CallerTokenVerifier(TokenVerifier):
    def __init__(self, sandbox: SandboxPrincipalResolver, *, oauth: ActionsOAuthProxy | None) -> None:
        # The challenge's resource_metadata URL lives under the OAuth proxy's public base URL,
        # where `ActionsOAuthProxy.get_routes` serves that metadata.
        super().__init__(base_url=oauth.base_url if oauth is not None else None)
        self._sandbox = sandbox
        self._oauth = oauth

    async def verify_token(self, token: str) -> CallerToken | None:
        if self._oauth is not None:
            grant = await self._oauth.authenticate(token)
            if grant is not None:
                principal = grant.principal()
                return CallerToken(
                    token=token,
                    client_id=principal.subject,
                    scopes=[],
                    principal=principal,
                    external_grant=grant.provenance(),
                )
            if self._oauth.targets_issuer(token):
                return None
        try:
            principal = workload_principal(await self._sandbox.resolve(token))
        except SandboxPrincipalRejectedError:
            return None
        return CallerToken(
            token=token, client_id=principal.subject, scopes=[], principal=principal, external_grant=None
        )

    def get_middleware(self) -> list[Middleware]:
        return [
            Middleware(AuthenticationMiddleware, backend=_SoleBearerBackend(self), on_error=_authority_unavailable),
            Middleware(AuthContextMiddleware),
        ]
