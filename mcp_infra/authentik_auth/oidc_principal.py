"""Verify an Authentik token response into a minimal OIDC principal."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt import PyJWK, PyJWKSet
from jwt.exceptions import InvalidTokenError, PyJWKError, PyJWKSetError, PyJWTError
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


class InvalidOidcPrincipalError(Exception):
    """The supplied token response cannot establish the expected principal."""

    def __init__(self) -> None:
        super().__init__("OIDC principal token is invalid")


class OidcPrincipalVerificationUnavailableError(Exception):
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


class _UnusableJwksError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _CachedSigningKeys:
    keys: tuple[PyJWK, ...]
    unusable_kids: frozenset[str]
    expires_at: float


def _is_loopback_url(parsed: SplitResult) -> bool:
    hostname = parsed.hostname
    if hostname is None:
        return False
    hostname = hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_oidc_url(value: str, *, field_name: str, allow_query: bool) -> None:
    parsed = urlsplit(value)
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError(f"{field_name} must be an absolute HTTPS URL or loopback HTTP URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
        or (parsed.scheme == "http" and not _is_loopback_url(parsed))
    ):
        raise ValueError(f"{field_name} must be an absolute HTTPS URL or loopback HTTP URL")


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
        _validate_oidc_url(expected_issuer, field_name="expected_issuer", allow_query=False)
        if not client_id.strip():
            raise ValueError("client_id must not be blank")
        if not jwks_uri:
            raise ValueError("OIDC discovery metadata must contain jwks_uri")
        _validate_oidc_url(jwks_uri, field_name="jwks_uri", allow_query=True)
        if signing_algorithms is None or isinstance(signing_algorithms, str):
            raise ValueError("OIDC discovery metadata must advertise signing algorithms")
        if any(not isinstance(algorithm, str) for algorithm in signing_algorithms):
            raise ValueError("OIDC signing algorithms must be strings")
        if _SIGNING_ALGORITHM not in signing_algorithms:
            raise ValueError(f"OIDC discovery metadata must advertise {_SIGNING_ALGORITHM}")

        self._issuer = expected_issuer
        self._client_id = client_id
        self._jwks_uri = jwks_uri
        self._cached_signing_keys: _CachedSigningKeys | None = None
        self._jwks_lock = asyncio.Lock()

    async def resolve(self, token_response: Mapping[str, Any]) -> VerifiedOidcPrincipal:
        """Return the issuer-scoped subject established by an Authentik access token."""
        try:
            token = _TokenResponse.model_validate(token_response).access_token
            header = jwt.get_unverified_header(token)
        except (InvalidTokenError, RecursionError, ValidationError, TypeError, ValueError):
            raise InvalidOidcPrincipalError from None

        kid = header.get("kid")
        if header.get("alg") != _SIGNING_ALGORITHM or "crit" in header or not isinstance(kid, str) or not kid.strip():
            raise InvalidOidcPrincipalError from None

        try:
            async with self._jwks_lock:
                signing_key = await asyncio.to_thread(self._signing_key, kid)
        except _UnknownSigningKeyError:
            raise InvalidOidcPrincipalError from None
        except (
            AttributeError,
            httpx.HTTPError,
            OSError,
            OverflowError,
            PyJWKError,
            PyJWKSetError,
            RecursionError,
            TypeError,
            UnicodeError,
            _UnusableJwksError,
            ValueError,
        ):
            raise OidcPrincipalVerificationUnavailableError from None

        # RFC 7517 makes a JWK's ``alg`` optional. PyJWT infers RS256 for an
        # otherwise valid RSA signing key; an explicit incompatible algorithm
        # or a non-RSA key makes the issuer's key set unusable for this contract.
        if (
            signing_key.algorithm_name != _SIGNING_ALGORITHM
            or signing_key.key_type != "RSA"
            or not isinstance(signing_key.key, RSAPublicKey)
        ):
            raise OidcPrincipalVerificationUnavailableError from None

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
        except (InvalidTokenError, OverflowError, RecursionError, TypeError, ValueError):
            raise InvalidOidcPrincipalError from None
        except PyJWTError:
            raise OidcPrincipalVerificationUnavailableError from None

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
            raise InvalidOidcPrincipalError from None
        return VerifiedOidcPrincipal(issuer=self._issuer, subject=subject)

    def _signing_key(self, kid: str) -> PyJWK:
        cached = self._cached_signing_keys
        key_set = cached if cached is not None and cached.expires_at > time.monotonic() else self._refresh_keys()
        if kid in key_set.unusable_kids:
            raise _UnusableJwksError
        if signing_key := self._match_kid(key_set.keys, kid):
            return signing_key
        refreshed = self._refresh_keys()
        if kid in refreshed.unusable_kids:
            raise _UnusableJwksError
        if signing_key := self._match_kid(refreshed.keys, kid):
            return signing_key
        raise _UnknownSigningKeyError

    def _refresh_keys(self) -> _CachedSigningKeys:
        response = httpx.get(self._jwks_uri, timeout=_JWKS_TIMEOUT_SECONDS, follow_redirects=False)
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict):
            raise _UnusableJwksError
        keys: list[PyJWK] = []
        unusable_kids: set[str] = set()
        for key in PyJWKSet.from_dict(document).keys:
            kid = key.key_id
            if key.public_key_use not in {"sig", None} or not isinstance(kid, str) or not kid.strip():
                continue
            if key.algorithm_name == _SIGNING_ALGORITHM and key.key_type == "RSA" and isinstance(key.key, RSAPublicKey):
                keys.append(key)
            else:
                unusable_kids.add(kid)
        if not keys:
            raise _UnusableJwksError
        refreshed = _CachedSigningKeys(
            keys=tuple(keys),
            unusable_kids=frozenset(unusable_kids),
            expires_at=time.monotonic() + _JWKS_CACHE_LIFESPAN_SECONDS,
        )
        self._cached_signing_keys = refreshed
        return refreshed

    @staticmethod
    def _match_kid(signing_keys: tuple[PyJWK, ...], kid: str) -> PyJWK | None:
        return next((key for key in signing_keys if key.key_id == kid), None)
