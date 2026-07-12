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
import time
from typing import Any
from urllib.parse import parse_qs

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
) -> Starlette:
    """Build a minimal OIDC provider supporting discovery, JWKS, authorize, and token.

    ``subject`` is the ``sub`` both tokens carry; ``extra_id_token_claims`` are merged into the id
    token (e.g. ``{"preferred_username": "agentydragon"}``) so a browser callback can read them.
    """
    jwks = build_jwks(public_key)
    extra_claims = extra_id_token_claims or {}
    pending_codes: dict[str, dict[str, Any]] = {}

    async def discovery(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": issuer_url,
                "authorization_endpoint": f"{issuer_url}/authorize",
                "token_endpoint": f"{issuer_url}/token",
                "jwks_uri": f"{issuer_url}/jwks",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "email", "profile", "offline_access"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
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
            "redirect_uri": params.get("redirect_uri", ""),
            "scope": params.get("scope", "openid"),
            "code_challenge": params.get("code_challenge"),
            "nonce": params.get("nonce"),
        }
        redirect_uri = params["redirect_uri"]
        state = params.get("state", "")
        return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=302)

    async def token(request: Request) -> JSONResponse:
        """Exchange auth code for tokens."""
        body = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        code = body.get("code", "")
        code_data = pending_codes.pop(code, None)
        if code_data is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        now = int(time.time())
        scope = code_data.get("scope", "openid")
        # A confidential client (authlib) authenticates with `client_secret_basic` — the client_id is
        # in the Basic header, not the body — so the id_token's `aud`/`azp` must be read from there or
        # authlib's azp validation rejects the token.
        client_id = body.get("client_id") or _basic_auth_client_id(request) or "test"
        base_claims = {"iss": issuer_url, "sub": subject, "iat": now, "exp": now + 3600, "scope": scope}
        access_token = sign_jwt(private_key, {**base_claims, "aud": body.get("resource", issuer_url)})
        id_claims = {**base_claims, "aud": client_id, "azp": client_id, **extra_claims}
        if code_data.get("nonce") is not None:
            id_claims["nonce"] = code_data["nonce"]
        id_token = sign_jwt(private_key, id_claims)
        return JSONResponse(
            {
                "access_token": access_token,
                "id_token": id_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": scope,
            }
        )

    return Starlette(
        routes=[
            Route("/.well-known/openid-configuration", discovery),
            Route("/jwks", jwks_endpoint),
            Route("/authorize", authorize),
            Route("/token", token, methods=["POST"]),
        ]
    )


def _basic_auth_client_id(request: Request) -> str | None:
    """The client_id from an HTTP Basic ``Authorization`` header, or None."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    return base64.b64decode(value).decode().partition(":")[0]
