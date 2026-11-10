"""ID tokens for the fixture's test operator, signed in the test process under the key the app pins."""

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agentplane.system_tests.deployment.rendered_fixture import OperatorTokenIssuer


@dataclass(frozen=True)
class OperatorTokens:
    issuer: OperatorTokenIssuer
    key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)

    @property
    def public_key(self) -> str:
        """The unpadded base64url key the app's `oidc.static_ed25519_keys` pins."""
        raw = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def mint(self, *, lifetime: timedelta) -> str:
        now = datetime.now(UTC)
        claims = {
            "iss": self.issuer.issuer,
            "aud": self.issuer.audience,
            "sub": self.issuer.subject,
            "preferred_username": self.issuer.username,
            "iat": now,
            "exp": now + lifetime,
        }
        return jwt.encode(claims, self.key, algorithm="EdDSA")
