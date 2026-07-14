"""Verify an Authentik token response into a minimal OIDC principal."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import jwt
from jwt import PyJWK, PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientError, PyJWKError, PyJWKSetError, PyJWTError
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

_SIGNING_ALGORITHM = "RS256"
_CLOCK_SKEW_SECONDS = 30
_JWKS_CACHE_LIFESPAN_SECONDS = 300
_JWKS_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class VerifiedOidcPrincipal:
    """The immutable identity established by a verified OIDC access token."""

    issuer: str
    subject: str


class InvalidOidcPrincipal(Exception):  # noqa: N818 - domain outcome named by the public contract
    """The supplied token response cannot establish the expected principal."""

    def __init__(self) -> None:
        super().__init__("OIDC principal token is invalid")


class OidcPrincipalVerificationUnavailable(Exception):  # noqa: N818 - domain outcome named by the public contract
    """The configured issuer's signing keys are currently unusable."""

    def __init__(self) -> None:
        super().__init__("OIDC principal verification is temporarily unavailable")


class _TokenResponse(BaseModel):
    """The token-endpoint fields used at the untyped OAuth response boundary."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    access_token: str
    token_type: str

    @field_validator("access_token")
    @classmethod
    def _nonblank_access_token(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("access_token must not be blank")
        return value

    @field_validator("token_type")
    @classmethod
    def _bearer_token_type(cls, value: str) -> str:
        if value.casefold() != "bearer":
            raise ValueError("token_type must be Bearer")
        return value


class _UnknownSigningKeyError(Exception):
    pass


class AuthentikOidcPrincipalResolver:
    """Verify Authentik access tokens using already-discovered OIDC metadata.

    Construction validates the discovery result once. Resolution performs no
    discovery request; the only network access is the bounded JWKS lookup.
    """

    def __init__(
        self,
        *,
        expected_issuer: str,
        discovered_issuer: str | None,
        jwks_uri: str | None,
        signing_algorithms: Collection[str] | None,
        client_id: str,
    ) -> None:
        if not expected_issuer or discovered_issuer != expected_issuer:
            raise ValueError("discovered issuer must exactly match expected_issuer")
        if not client_id.strip():
            raise ValueError("client_id must not be blank")
        if not jwks_uri:
            raise ValueError("OIDC discovery metadata must contain jwks_uri")
        parsed_jwks_uri = urlparse(jwks_uri)
        if parsed_jwks_uri.scheme not in {"http", "https"} or not parsed_jwks_uri.netloc:
            raise ValueError("jwks_uri must be an absolute HTTP(S) URL")
        if signing_algorithms is None or isinstance(signing_algorithms, str):
            raise ValueError("OIDC discovery metadata must advertise signing algorithms")
        if any(not isinstance(algorithm, str) for algorithm in signing_algorithms):
            raise ValueError("OIDC signing algorithms must be strings")
        if _SIGNING_ALGORITHM not in signing_algorithms:
            raise ValueError(f"OIDC discovery metadata must advertise {_SIGNING_ALGORITHM}")

        self._issuer = expected_issuer
        self._client_id = client_id
        self._jwks_client = PyJWKClient(
            jwks_uri,
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=_JWKS_CACHE_LIFESPAN_SECONDS,
            timeout=_JWKS_TIMEOUT_SECONDS,
        )
        self._jwks_lock = asyncio.Lock()

    async def resolve(self, token_response: Mapping[str, Any]) -> VerifiedOidcPrincipal:
        """Return the issuer-scoped subject established by an Authentik access token."""
        try:
            token = _TokenResponse.model_validate(token_response).access_token
            header = jwt.get_unverified_header(token)
        except (InvalidTokenError, ValidationError, TypeError, ValueError):
            raise InvalidOidcPrincipal from None

        kid = header.get("kid")
        if header.get("alg") != _SIGNING_ALGORITHM or "crit" in header or not isinstance(kid, str) or not kid.strip():
            raise InvalidOidcPrincipal from None

        try:
            async with self._jwks_lock:
                signing_key = await asyncio.to_thread(self._signing_key, kid)
        except _UnknownSigningKeyError:
            raise InvalidOidcPrincipal from None
        except (json.JSONDecodeError, OSError, PyJWKClientError, PyJWKError, PyJWKSetError, UnicodeError):
            raise OidcPrincipalVerificationUnavailable from None

        # RFC 7517 makes a JWK's ``alg`` optional. PyJWT infers RS256 for an
        # otherwise valid RSA signing key; an explicit incompatible algorithm
        # or a non-RSA key makes the issuer's key set unusable for this contract.
        if signing_key.algorithm_name != _SIGNING_ALGORITHM or signing_key.key_type != "RSA":
            raise OidcPrincipalVerificationUnavailable from None

        try:
            claims = await asyncio.to_thread(
                jwt.decode,
                token,
                signing_key.key,
                algorithms=[_SIGNING_ALGORITHM],
                audience=self._client_id,
                issuer=self._issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "aud", "azp", "exp", "iat", "sub"], "strict_aud": True},
            )
        except (InvalidTokenError, TypeError, ValueError):
            raise InvalidOidcPrincipal from None
        except PyJWTError:
            raise OidcPrincipalVerificationUnavailable from None

        issued_at = claims["iat"]
        expires_at = claims["exp"]
        subject = claims["sub"]
        if (
            type(issued_at) is not int
            or type(expires_at) is not int
            or expires_at <= issued_at
            or not isinstance(claims["iss"], str)
            or claims["iss"] != self._issuer
            or not isinstance(claims["aud"], str)
            or claims["aud"] != self._client_id
            or not isinstance(claims["azp"], str)
            or claims["azp"] != self._client_id
            or not isinstance(subject, str)
            or not subject.strip()
        ):
            raise InvalidOidcPrincipal from None
        return VerifiedOidcPrincipal(issuer=self._issuer, subject=subject)

    def _signing_key(self, kid: str) -> PyJWK:
        signing_keys = self._jwks_client.get_signing_keys()
        if signing_key := self._jwks_client.match_kid(signing_keys, kid):
            return signing_key
        refreshed_signing_keys = self._jwks_client.get_signing_keys(refresh=True)
        if signing_key := self._jwks_client.match_kid(refreshed_signing_keys, kid):
            return signing_key
        raise _UnknownSigningKeyError
