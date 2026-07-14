"""Contract tests for the FastMCP JWT verifier used by Authentik auth."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_bazel
from fastmcp.server.auth.auth import AccessToken, AuthProvider, MultiAuth
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from joserfc import jwk

from mcp_infra.authentik_auth.auth import (
    DEFAULT_VALID_SCOPES,
    AuthentikAuthConfig,
    DirectJwtTrust,
    build_authentik_auth,
)

JWKS_URI = "https://auth.example.test/application/o/interactive/jwks/"
MACHINE_A_ISSUER = "https://auth.example.test/application/o/machine-a/"
MACHINE_B_ISSUER = "https://auth.example.test/application/o/machine-b/"


class _RejectingOIDCProxy(AuthProvider):
    """Stand-in for the interactive server while direct JWT verification is exercised."""

    def __init__(self, **_: object) -> None:
        super().__init__()
        self.oidc_config = OIDCConfiguration(strict=False, jwks_uri=JWKS_URI)
        self.default_scopes: list[str] | None = None

    def update_default_scopes(self, scopes: list[str]) -> None:
        self.default_scopes = scopes

    async def verify_token(self, _token: str) -> AccessToken | None:
        return None


@dataclass(frozen=True)
class _JWTContractHarness:
    auth: MultiAuth
    signing_keys: dict[str, RSAKeyPair]
    jwks_requests: list[httpx.Request]

    def create_token(
        self,
        *,
        issuer: str = MACHINE_A_ISSUER,
        audience: str | list[str] = "machine-a",
        scopes: list[str] | None = None,
        signing_kid: str = "primary",
        header_kid: str | None = None,
    ) -> str:
        return self.signing_keys[signing_kid].create_token(
            subject="agent-123",
            issuer=issuer,
            audience=audience,
            scopes=scopes if scopes is not None else ["openid", "profile"],
            kid=header_kid or signing_kid,
        )


@pytest.fixture(scope="module")
def signing_keys() -> dict[str, RSAKeyPair]:
    return {"primary": RSAKeyPair.generate(), "rotated": RSAKeyPair.generate()}


@pytest.fixture
async def jwt_contract_harness(
    monkeypatch: pytest.MonkeyPatch, signing_keys: dict[str, RSAKeyPair]
) -> AsyncIterator[_JWTContractHarness]:
    jwks_requests: list[httpx.Request] = []

    def handle_jwks(request: httpx.Request) -> httpx.Response:
        jwks_requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "keys": [
                    jwk.import_key(key_pair.public_key, "RSA").as_dict(kid=kid, use="sig", alg="RS256")
                    for kid, key_pair in signing_keys.items()
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_jwks)) as jwks_client:

        def build_verifier(
            *,
            jwks_uri: str,
            issuer: str | list[str] | None,
            audience: str | list[str] | None,
            required_scopes: list[str] | None,
        ) -> JWTVerifier:
            return JWTVerifier(
                jwks_uri=jwks_uri,
                issuer=issuer,
                audience=audience,
                required_scopes=required_scopes,
                http_client=jwks_client,
            )

        monkeypatch.setattr("mcp_infra.authentik_auth.auth.ResilientOIDCProxy", _RejectingOIDCProxy)
        monkeypatch.setattr("mcp_infra.authentik_auth.auth.JWTVerifier", build_verifier)

        auth = build_authentik_auth(
            AuthentikAuthConfig(
                oidc_issuer="https://auth.example.test/application/o/interactive/",
                oidc_client_id="interactive-client",
                oidc_client_secret="secret",
                public_base_url="https://mcp.example.test",
                direct_jwt_trusts=(
                    DirectJwtTrust(
                        issuer=MACHINE_A_ISSUER,
                        audiences=("machine-a", "machine-a-alternate"),
                        required_scopes=("openid", "profile"),
                    ),
                    DirectJwtTrust(issuer=MACHINE_B_ISSUER, audiences=("machine-b",), required_scopes=("openid",)),
                ),
            )
        )
        assert isinstance(auth, MultiAuth)
        assert isinstance(auth.server, _RejectingOIDCProxy)
        assert auth.server.default_scopes == DEFAULT_VALID_SCOPES
        yield _JWTContractHarness(auth=auth, signing_keys=signing_keys, jwks_requests=jwks_requests)

    assert jwks_requests
    assert {str(request.url) for request in jwks_requests} == {JWKS_URI}


@pytest.mark.parametrize(
    ("issuer", "signing_kid"),
    [
        pytest.param(MACHINE_A_ISSUER, "primary", id="trailing-issuer-primary-key"),
        pytest.param(MACHINE_A_ISSUER.rstrip("/"), "rotated", id="bare-issuer-rotated-key"),
    ],
)
async def test_accepts_configured_issuer_variants_audience_lists_and_jwks_kids(
    jwt_contract_harness: _JWTContractHarness, issuer: str, signing_kid: str
) -> None:
    scopes = ["openid", "profile", "extra"]
    access_token = await jwt_contract_harness.auth.verify_token(
        jwt_contract_harness.create_token(
            issuer=issuer, audience=["unrelated", "machine-a-alternate"], scopes=scopes, signing_kid=signing_kid
        )
    )

    assert access_token is not None
    assert access_token.client_id == "agent-123"
    assert access_token.scopes == scopes


async def test_rejects_wrong_issuer(jwt_contract_harness: _JWTContractHarness) -> None:
    access_token = await jwt_contract_harness.auth.verify_token(
        jwt_contract_harness.create_token(issuer="https://attacker.example.test/application/o/machine-a/")
    )

    assert access_token is None


async def test_rejects_cross_audience_token(jwt_contract_harness: _JWTContractHarness) -> None:
    access_token = await jwt_contract_harness.auth.verify_token(
        jwt_contract_harness.create_token(issuer=MACHINE_A_ISSUER, audience="machine-b")
    )

    assert access_token is None


async def test_rejects_missing_required_scope(jwt_contract_harness: _JWTContractHarness) -> None:
    access_token = await jwt_contract_harness.auth.verify_token(jwt_contract_harness.create_token(scopes=["openid"]))

    assert access_token is None


async def test_rejects_token_whose_kid_selects_the_wrong_key(jwt_contract_harness: _JWTContractHarness) -> None:
    access_token = await jwt_contract_harness.auth.verify_token(
        jwt_contract_harness.create_token(signing_kid="primary", header_kid="rotated")
    )

    assert access_token is None


if __name__ == "__main__":
    pytest_bazel.main()
