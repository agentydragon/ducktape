"""Pinned JWT verification for the configured Action audience.

Authentik's target-provider policy decides who may obtain a token. The Action Service verifies the
resulting issuer, audience, signature, and lifetime without maintaining a second user allowlist.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from agentplane.action_service.models import OperatorPrincipal
from mcp_infra.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    DexOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalResolver,
    OidcPrincipalVerificationUnavailableError,
)


class OperatorTokenProfile(StrEnum):
    AUTHENTIK = "authentik"
    DEX = "dex"


class OperatorOidcSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    audience: str
    jwks_uri: str
    token_profile: OperatorTokenProfile = OperatorTokenProfile.AUTHENTIK

    def resolver(self) -> OidcPrincipalResolver:
        # These are reviewed configuration pins, not metadata taken from the presented token.
        resolver = (
            AuthentikOidcPrincipalResolver
            if self.token_profile is OperatorTokenProfile.AUTHENTIK
            else DexOidcPrincipalResolver
        )
        return resolver(
            expected_issuer=self.issuer,
            discovered_issuer=self.issuer,
            jwks_uri=self.jwks_uri,
            signing_algorithms=["RS256"],
            client_id=self.audience,
        )


class OidcOperatorAuthenticator:
    def __init__(self, settings: OperatorOidcSettings) -> None:
        self._resolver = settings.resolver()

    async def authenticate(self, token: str) -> OperatorPrincipal | None:
        try:
            identity = await self._resolver.resolve({"access_token": token, "token_type": "Bearer"})
        except InvalidOidcPrincipalError, OidcPrincipalVerificationUnavailableError:
            return None
        return OperatorPrincipal(issuer=identity.issuer, subject=identity.subject)
