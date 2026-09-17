"""Verify the MCP transport's bearer as a workload token or an external OAuth grant.

A workload token proves the ServiceAccount its Pod runs as -- the same principal an OAuth grant
acting as that ServiceAccount resolves to, so one `ActionPolicyBinding` covers both ways of
arriving, and `CALLER_LABEL` on the account is what admits either. The grant path carries that
check inside `ConnectionAuthority.resolve`, which runs on every admission; the workload path has no
such authority to consult, so it asks the informer's index here.

FastMCP runs this verifier on every transport request (`RequireAuthMiddleware` around `/mcp`) and
hands tools the resulting `CallerToken` through `CurrentAccessToken`; no identity rides on the
request. Deviation from FastMCP's stock bearer backend: exactly one Authorization header is
accepted, as on the workload HTTP routes, and a grant-authority outage answers with its own 503
instead of a false `invalid_token`.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, BearerAuthBackend
from starlette.authentication import AuthCredentials, AuthenticationError
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from x.agentplane.action_service.models import CallerPrincipal, ExternalGrantProvenance
from x.agentplane.action_service.oauth import ActionsOAuthProxy
from x.agentplane.action_service.policy_informer import PolicyIndex
from x.agentplane.sandbox_auth.bearer import sole_header
from x.agentplane.sandbox_auth.principal import SandboxPrincipalRejectedError, SandboxPrincipalResolver

logger = logging.getLogger(__name__)


class CallerToken(AccessToken):
    """The verified identity FastMCP carries for one transport request."""

    principal: CallerPrincipal
    external_grant: ExternalGrantProvenance | None


class CallerAuthorityUnavailableError(AuthenticationError):
    """The grant authority could not answer, so the bearer was neither accepted nor refused."""

    def __init__(self, response: HTTPException) -> None:
        super().__init__(response.detail)
        self.response = response


class _SoleBearerBackend(BearerAuthBackend):
    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, AuthenticatedUser] | None:
        # The count rule is shared; the parse is not. This door also admits OAuth access tokens,
        # whose spelling belongs to whoever issued them, so FastMCP reads the value.
        if sole_header(conn.headers.getlist("authorization")) is None:
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
    def __init__(
        self, sandbox: SandboxPrincipalResolver, *, callers: PolicyIndex, oauth: ActionsOAuthProxy | None
    ) -> None:
        # The challenge's resource_metadata URL lives under the OAuth proxy's public base URL,
        # where `ActionsOAuthProxy.get_routes` serves that metadata.
        super().__init__(base_url=oauth.base_url if oauth is not None else None)
        self._sandbox = sandbox
        self._callers = callers
        self._oauth = oauth

    async def verify_token(self, token: str) -> CallerToken | None:
        if self._oauth is not None:
            grant = await self._oauth.authenticate(token)
            if grant is not None:
                provenance = grant.provenance()
                return CallerToken(
                    token=token,
                    client_id=provenance.client_id,
                    scopes=[],
                    principal=grant.principal(),
                    external_grant=provenance,
                )
            if self._oauth.targets_issuer(token):
                return None
        try:
            account = (await self._sandbox.resolve_workload(token)).account
        except SandboxPrincipalRejectedError:
            return None
        # As opaque to the caller as an unknown bearer: it already knows which account it holds, and
        # an attacker should not learn from the difference that the account exists.
        caller = self._callers.admit(account)
        if caller is None:
            return None
        return CallerToken(
            token=token,
            # FastMCP's field names an OAuth client, and a workload bearer has none. The account is
            # what a person reading a FastMCP log needs; the authoritative one is `provenance`.
            client_id=f"{account.namespace}/{account.name}",
            scopes=[],
            principal=caller,
            external_grant=None,
        )

    def get_middleware(self) -> list[Middleware]:
        return [
            Middleware(AuthenticationMiddleware, backend=_SoleBearerBackend(self), on_error=_authority_unavailable),
            Middleware(AuthContextMiddleware),
        ]
