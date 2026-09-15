"""Pinned Authentik JWT verification for the Authentik-authorized Action audience.

Authentik's target-provider policy decides who may obtain a token. The Action Service verifies the
resulting issuer, audience, signature, and lifetime without maintaining a second user allowlist.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from mcp_infra.authentik_auth.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
)
from x.agentplane.action_service.models import Principal, PrincipalRole


class OperatorOidcSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    audience: str
    jwks_uri: str

    def resolver(self) -> AuthentikOidcPrincipalResolver:
        # These are reviewed configuration pins, not metadata taken from the presented token.
        return AuthentikOidcPrincipalResolver(
            expected_issuer=self.issuer,
            discovered_issuer=self.issuer,
            jwks_uri=self.jwks_uri,
            signing_algorithms=["RS256"],
            client_id=self.audience,
        )


class OidcOperatorAuthenticator:
    def __init__(self, settings: OperatorOidcSettings) -> None:
        self._resolver = settings.resolver()

    async def authenticate(self, token: str) -> Principal | None:
        try:
            identity = await self._resolver.resolve({"access_token": token, "token_type": "Bearer"})
        except (InvalidOidcPrincipalError, OidcPrincipalVerificationUnavailableError):
            return None
        return Principal(issuer=identity.issuer, subject=identity.subject, role=PrincipalRole.OPERATOR)
