"""ID tokens the app accepts as the fixture's test operator: signed in the test process under the key the app
pins, or issued by the MCP fixtures' Dex."""

import base64
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agentplane.system_tests.deployment.rendered_fixture import DexPasswordGrant, OperatorTokenIssuer


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


def dex_id_token(grant: DexPasswordGrant, *, ca_bundle: Path) -> str:
    """The operator's ID token from Dex's password grant, for the app's client; Dex sets its lifetime."""
    with httpx.Client(verify=ssl.create_default_context(cafile=ca_bundle)) as client:
        discovery = client.get(f"{grant.issuer}/.well-known/openid-configuration").raise_for_status().json()
        token = client.post(
            discovery["token_endpoint"],
            auth=(grant.client_id, grant.client_secret),
            data={
                "grant_type": "password",
                "scope": "openid profile",
                "username": grant.username,
                "password": grant.password,
            },
        ).raise_for_status()
    return str(token.json()["id_token"])
