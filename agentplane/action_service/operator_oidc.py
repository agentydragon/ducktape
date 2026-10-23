"""Pinned JWT verification for the configured Action audience.

Authentik's target-provider policy decides who may obtain a token. The Action Service verifies the
resulting issuer, audience, signature, and lifetime without maintaining a second user allowlist.
"""

from __future__ import annotations

from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings, OperatorTokenProfile
from util.oidc_principal import AuthentikOidcPrincipalResolver, DexOidcPrincipalResolver, OidcPrincipalResolver


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
