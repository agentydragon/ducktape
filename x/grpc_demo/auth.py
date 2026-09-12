"""Application-owned verification of Authentik OIDC access tokens."""

from __future__ import annotations

import json
import os
from collections.abc import Collection, Mapping
from urllib.request import Request, urlopen

import jwt
from jwt.exceptions import PyJWTError


class InvalidAccessTokenError(Exception):
    """The supplied bearer token is not a valid access token for this app."""


def _discover_jwks_uri(issuer: str) -> str:
    """Resolve the issuer's signing-key endpoint from OIDC discovery."""
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    request = Request(discovery_url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        document = json.load(response)
    jwks_uri = document.get("jwks_uri") if isinstance(document, dict) else None
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise RuntimeError(f"OIDC discovery document at {discovery_url} has no jwks_uri")
    return jwks_uri


class OidcTokenVerifier:
    """Verify JWT access tokens against an OIDC issuer's rotating JWKS."""

    def __init__(self, *, issuer: str, audience: str, jwks_uri: str, algorithms: Collection[str] = ("RS256",)) -> None:
        if not issuer:
            raise ValueError("issuer must not be empty")
        if not audience:
            raise ValueError("audience must not be empty")
        if not jwks_uri:
            raise ValueError("jwks_uri must not be empty")
        if not algorithms:
            raise ValueError("algorithms must not be empty")
        self._issuer = issuer
        self._audience = audience
        self._algorithms = tuple(algorithms)
        self._jwks_client = jwt.PyJWKClient(jwks_uri)

    @classmethod
    def from_environment(cls) -> OidcTokenVerifier:
        """Build a verifier from the demo's deployment configuration."""
        issuer = os.environ.get("GRPC_DEMO_OIDC_ISSUER", "").strip()
        audience = os.environ.get("GRPC_DEMO_OIDC_AUDIENCE", "").strip()
        if not issuer or not audience:
            raise RuntimeError("GRPC_DEMO_OIDC_ISSUER and GRPC_DEMO_OIDC_AUDIENCE are required")

        jwks_uri = os.environ.get("GRPC_DEMO_OIDC_JWKS_URI", "").strip() or _discover_jwks_uri(issuer)
        configured_algorithms = os.environ.get("GRPC_DEMO_OIDC_ALGORITHMS", "RS256")
        algorithms = tuple(algorithm.strip() for algorithm in configured_algorithms.split(",") if algorithm.strip())
        return cls(issuer=issuer, audience=audience, jwks_uri=jwks_uri, algorithms=algorithms)

    def __call__(self, token: str) -> Mapping[str, object]:
        """Return verified claims or raise without exposing token details."""
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in self._algorithms or "crit" in header:
                raise InvalidAccessTokenError
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["iss", "aud", "exp", "iat", "sub"], "strict_aud": True},
            )
        except (InvalidAccessTokenError, KeyError, PyJWTError, RecursionError, TypeError, ValueError) as error:
            raise InvalidAccessTokenError from error

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise InvalidAccessTokenError
        return claims
