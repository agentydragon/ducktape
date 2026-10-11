"""Bearers for exercising the Action Service's workload and operator boundaries in tests."""

from __future__ import annotations

import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from agentplane.action_service.models import OperatorPrincipal
from agentplane.action_service.operator_oidc import OidcOperatorAuthenticator
from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings, OperatorTokenProfile
from util.testing.mock_oidc import sign_jwt

# The public placeholder a sandbox sends; central egress substitutes the real workload bearer.
WORKLOAD_CREDENTIAL_PLACEHOLDER = "agentplane-credential-agentplane-workload"

OPERATOR_AUDIENCE = "test-operator-audience"


@dataclass(frozen=True)
class OperatorIdp:
    """A served mock IdP, for which the production operator authenticator admits signed bearers."""

    issuer: str
    signing_key: RSAPrivateKey

    def authenticator(self) -> OidcOperatorAuthenticator:
        return OidcOperatorAuthenticator(
            OperatorOidcSettings(
                issuer=self.issuer,
                audience=OPERATOR_AUDIENCE,
                jwks_uri=f"{self.issuer}/jwks",
                token_profile=OperatorTokenProfile.AUTHENTIK,
            )
        )

    def principal(self, subject: str) -> OperatorPrincipal:
        return OperatorPrincipal(issuer=self.issuer, subject=subject)

    def bearer(self, subject: str) -> str:
        """A signed access token `authenticator()` admits as `principal(subject)`."""
        now = int(time.time())
        return sign_jwt(
            self.signing_key,
            {
                "iss": self.issuer,
                "aud": OPERATOR_AUDIENCE,
                "azp": OPERATOR_AUDIENCE,
                "sub": subject,
                "iat": now,
                "exp": now + 3600,
            },
        )
