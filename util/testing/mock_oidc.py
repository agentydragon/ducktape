"""A minimal, hermetic OIDC provider for tests — no real Authentik needed.

Serves discovery, JWKS, ``/authorize`` (issues a code immediately, simulating login+consent) and
``/token`` (exchanges the code for signed access + id tokens). Enough to exercise both an MCP
``OIDCProxy`` (DCR/PKCE) and a browser authorization-code client (authlib) end-to-end: the id token
echoes the request ``nonce`` and carries a configurable ``sub`` plus any ``extra_id_token_claims``
(e.g. ``preferred_username``), which is what an authlib callback validates and reads.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey, RSAPublicNumbers
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

RSAKeyPair = tuple[RSAPrivateKey, RSAPublicKey]


def generate_rsa_keypair() -> RSAKeyPair:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def build_jwks(public_key: RSAPublicKey) -> dict[str, Any]:
    """Build a single-key JWKS document from an RSA public key."""
    numbers: RSAPublicNumbers = public_key.public_numbers()

    def _b64url(n: int, length: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    e_bytes = (numbers.e.bit_length() + 7) // 8
    n_bytes = (numbers.n.bit_length() + 7) // 8
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": "test-key",
                "n": _b64url(numbers.n, n_bytes),
                "e": _b64url(numbers.e, e_bytes),
            }
        ]
    }


def sign_jwt(private_key: RSAPrivateKey, claims: dict[str, Any]) -> str:
    """Sign a JWT with the mock provider's RSA key."""
    return pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})


def build_mock_oidc_app(
    *,
    issuer_url: str,
    private_key: RSAPrivateKey,
    public_key: RSAPublicKey,
    subject: str = "test-user",
    extra_id_token_claims: dict[str, Any] | None = None,
    authentik_compatible: bool = False,
    exchange_token_audience: str | None = None,
    expected_exchange_client_id: str | None = None,
    on_client_assertion: Callable[[str], None] | None = None,
    client_assertion_validator: Callable[[str], bool] | None = None,
) -> Starlette:
    """Build a minimal OIDC provider supporting discovery, JWKS, authorize, and token.

    ``subject`` is the ``sub`` both tokens carry; ``extra_id_token_claims`` are merged into the id
    token (e.g. ``{"preferred_username": "agentydragon"}``) so a browser callback can read them.

    ``authentik_compatible`` serves a per-provider issuer below ``/application/o/<slug>/`` while
    advertising Authentik's shared ``/application/o/{authorize,token}/`` endpoints. The shared token
    endpoint additionally accepts Authentik's RFC 7521 ``client_credentials`` exchange: it verifies
    the signed ``client_assertion`` and mints a new JWT for the same subject. Tests may use
    ``on_client_assertion`` to inspect the assertion that crossed that protocol boundary and
    ``client_assertion_validator`` to make the exchange endpoint accept or reject a valid assertion.
    ``expected_exchange_client_id`` fails the exchange if the proxy-provider client is miswired.
    """
    issuer_base = issuer_url.rstrip("/")
    parsed_issuer = urlparse(issuer_base)
    issuer_path = parsed_issuer.path.rstrip("/")
    if authentik_compatible:
        prefix, marker, provider_slug = issuer_path.rpartition("/application/o/")
        if not marker or not provider_slug or "/" in provider_slug:
            raise ValueError("authentik-compatible issuer must end in /application/o/<slug>/")
        shared_path = f"{prefix}{marker}"
        authorization_path = f"{shared_path}authorize/"
        token_path = f"{shared_path}token/"
        jwks_path = f"{issuer_path}/jwks/"
        canonical_issuer = f"{issuer_base}/"
    else:
        authorization_path = f"{issuer_path}/authorize"
        token_path = f"{issuer_path}/token"
        jwks_path = f"{issuer_path}/jwks"
        canonical_issuer = issuer_base

    def _absolute(path: str) -> str:
        return urlunparse(parsed_issuer._replace(path=path, params="", query="", fragment=""))

    authorization_url = _absolute(authorization_path)
    token_url = _absolute(token_path)
    jwks_url = _absolute(jwks_path)
    jwks = build_jwks(public_key)
    extra_claims = extra_id_token_claims or {}
    pending_codes: dict[str, dict[str, Any]] = {}
    refresh_tokens: dict[str, dict[str, Any]] = {}

    async def discovery(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": canonical_issuer,
                "authorization_endpoint": authorization_url,
                "token_endpoint": token_url,
                "jwks_uri": jwks_url,
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "email", "profile", "offline_access"],
                "grant_types_supported": [
                    "authorization_code",
                    "refresh_token",
                    *(["client_credentials"] if authentik_compatible else []),
                ],
                "code_challenge_methods_supported": ["S256"],
            }
        )

    async def jwks_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(jwks)

    async def authorize(request: Request) -> RedirectResponse:
        """Simulate user consent + login by immediately issuing an auth code."""
        params = dict(request.query_params)
        code = hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:16]
        pending_codes[code] = {
            "client_id": params.get("client_id", "test"),
            "redirect_uri": params.get("redirect_uri", ""),
            "scope": params.get("scope", "openid"),
            "code_challenge": params.get("code_challenge"),
            "nonce": params.get("nonce"),
            "resource": params.get("resource"),
        }
        redirect_uri = params["redirect_uri"]
        state = params.get("state", "")
        return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=302)

    def _token_response(
        *, client_id: str, scope: str, token_subject: str, nonce: str | None = None, resource: str | None = None
    ) -> dict[str, Any]:
        now = int(time.time())
        base_claims = {"iss": canonical_issuer, "sub": token_subject, "iat": now, "exp": now + 3600, "scope": scope}
        access_claims = (
            {**base_claims, "aud": client_id, "azp": client_id}
            if authentik_compatible
            else {**base_claims, "aud": resource or canonical_issuer}
        )
        access_token = sign_jwt(private_key, access_claims)
        id_claims = {**base_claims, "aud": client_id, "azp": client_id, **extra_claims}
        if nonce is not None:
            id_claims["nonce"] = nonce
        refresh_token = secrets.token_urlsafe(24)
        refresh_tokens[refresh_token] = {"client_id": client_id, "scope": scope, "subject": token_subject}
        return {
            "access_token": access_token,
            "id_token": sign_jwt(private_key, id_claims),
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": scope,
        }

    async def token(request: Request) -> JSONResponse:
        """Issue authorization-code, refresh, and Authentik-style exchanged tokens."""
        body = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        grant_type = body.get("grant_type", "authorization_code")
        if grant_type == "client_credentials":
            assertion = body.get("client_assertion")
            if (
                body.get("client_assertion_type") != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                or assertion is None
            ):
                return JSONResponse({"error": "invalid_client"}, status_code=400)
            try:
                assertion_claims = pyjwt.decode(
                    assertion, public_key, algorithms=["RS256"], issuer=canonical_issuer, options={"verify_aud": False}
                )
            except pyjwt.PyJWTError:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if on_client_assertion is not None:
                on_client_assertion(assertion)
            if client_assertion_validator is not None and not client_assertion_validator(assertion):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            client_id = body.get("client_id") or _basic_auth_client_id(request) or "test"
            if expected_exchange_client_id is not None and client_id != expected_exchange_client_id:
                return JSONResponse({"error": "invalid_client"}, status_code=400)
            scope = body.get("scope", "openid")
            now = int(time.time())
            exchanged_claims = {
                "iss": canonical_issuer,
                "sub": assertion_claims["sub"],
                "iat": now,
                "exp": now + 3600,
                "scope": scope,
                "aud": exchange_token_audience or client_id,
            }
            return JSONResponse(
                {
                    "access_token": sign_jwt(private_key, exchanged_claims),
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": scope,
                }
            )

        if grant_type == "refresh_token":
            refresh_data = refresh_tokens.get(body.get("refresh_token", ""))
            if refresh_data is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            client_id = body.get("client_id") or _basic_auth_client_id(request) or refresh_data["client_id"]
            return JSONResponse(
                _token_response(
                    client_id=client_id,
                    scope=body.get("scope", refresh_data["scope"]),
                    token_subject=refresh_data["subject"],
                    resource=body.get("resource"),
                )
            )

        if grant_type != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        code = body.get("code", "")
        code_data = pending_codes.pop(code, None)
        if code_data is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if body.get("redirect_uri", "") != code_data["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if challenge := code_data.get("code_challenge"):
            verifier = body.get("code_verifier", "")
            actual = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if actual != challenge:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
        # A confidential client (authlib) authenticates with `client_secret_basic` — the client_id is
        # in the Basic header, not the body — so the id_token's `aud`/`azp` must be read from there or
        # authlib's azp validation rejects the token.
        client_id = body.get("client_id") or _basic_auth_client_id(request) or "test"
        if client_id != code_data["client_id"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(
            _token_response(
                client_id=client_id,
                scope=code_data["scope"],
                token_subject=subject,
                nonce=code_data.get("nonce"),
                resource=body.get("resource") or code_data.get("resource"),
            )
        )

    return Starlette(
        routes=[
            Route(f"{issuer_path}/.well-known/openid-configuration", discovery),
            Route(jwks_path, jwks_endpoint),
            Route(authorization_path, authorize),
            Route(token_path, token, methods=["POST"]),
        ]
    )


def _basic_auth_client_id(request: Request) -> str | None:
    """The client_id from an HTTP Basic ``Authorization`` header, or None."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    return base64.b64decode(value).decode().partition(":")[0]
