"""Pinned JWT verification for the configured Action audience.

Authentik's target-provider policy decides who may obtain a token. The Action Service verifies the
resulting issuer, audience, signature, and lifetime without maintaining a second user allowlist.
"""

from __future__ import annotations

from agentplane.action_service.models import OperatorPrincipal
from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings, OperatorTokenProfile
from mcp_infra.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    DexOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalResolver,
    OidcPrincipalVerificationUnavailableError,
)


def resolver_for(settings: OperatorOidcSettings) -> OidcPrincipalResolver:
    # These are reviewed configuration pins, not metadata taken from the presented token.
    resolver = (
        AuthentikOidcPrincipalResolver
        if settings.token_profile is OperatorTokenProfile.AUTHENTIK
        else DexOidcPrincipalResolver
    )
    return resolver(
        expected_issuer=settings.issuer,
        discovered_issuer=settings.issuer,
        jwks_uri=settings.jwks_uri,
        signing_algorithms=["RS256"],
        client_id=settings.audience,
    )


class OidcOperatorAuthenticator:
    def __init__(self, settings: OperatorOidcSettings) -> None:
        self._resolver = resolver_for(settings)

    async def authenticate(self, token: str) -> OperatorPrincipal | None:
        try:
            identity = await self._resolver.resolve({"access_token": token, "token_type": "Bearer"})
        except InvalidOidcPrincipalError, OidcPrincipalVerificationUnavailableError:
            return None
        return OperatorPrincipal(issuer=identity.issuer, subject=identity.subject)
