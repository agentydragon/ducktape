"""Behavioral tests for application-owned OIDC access-token validation."""

import time

import jwt
import pytest
import pytest_bazel
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey, generate_private_key

from x.grpc_demo.auth import InvalidAccessTokenError, OidcTokenVerifier


class _SigningKey:
    def __init__(self, key: RSAPublicKey) -> None:
        self.key = key


def _token(private_key: RSAPrivateKey, **claims: object) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "https://auth.example.com/application/o/grpc-demo/",
            "aud": "grpc-demo",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
            **claims,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "demo-key"},
    )


def _verifier(monkeypatch: pytest.MonkeyPatch) -> tuple[OidcTokenVerifier, RSAPrivateKey]:
    private_key = generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    def signing_key(_client: jwt.PyJWKClient, _token: str) -> _SigningKey:
        return _SigningKey(public_key)

    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", signing_key)
    return (
        OidcTokenVerifier(
            issuer="https://auth.example.com/application/o/grpc-demo/",
            audience="grpc-demo",
            jwks_uri="https://auth.example.com/application/o/grpc-demo/jwks/",
        ),
        private_key,
    )


def test_verifier_accepts_a_signed_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier, private_key = _verifier(monkeypatch)

    claims = verifier(_token(private_key))

    assert claims["sub"] == "alice"


def test_verifier_rejects_a_token_for_another_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier, private_key = _verifier(monkeypatch)

    with pytest.raises(InvalidAccessTokenError):
        verifier(_token(private_key, aud="another-service"))


if __name__ == "__main__":
    pytest_bazel.main()
