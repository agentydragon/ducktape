"""Security contract for Authentik access-token principal verification."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import pytest_bazel
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_infra.authentik_auth.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipal,
    OidcPrincipalVerificationUnavailable,
    VerifiedOidcPrincipal,
)
from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

_ISSUER = "https://auth.example.test/application/o/haku-mcp/"
_CLIENT_ID = "haku-mcp"
_SUBJECT = "authentik-user-id"


@dataclass(frozen=True, slots=True)
class _SigningKey:
    kid: str
    private: RSAPrivateKey
    public: RSAPublicKey


@dataclass(slots=True)
class _JwksState:
    document: Any
    status_code: int = 200
    raw_body: bytes | None = None
    requests: int = 0

    async def serve(self, request: Request) -> Response:
        self.requests += 1
        if self.raw_body is not None:
            return Response(self.raw_body, status_code=self.status_code, media_type="application/json")
        return JSONResponse(self.document, status_code=self.status_code)


def _new_signing_key(kid: str) -> _SigningKey:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _SigningKey(kid=kid, private=private, public=private.public_key())


def _jwk(key: _SigningKey, *, algorithm: str = "RS256") -> dict[str, Any]:
    value = cast(dict[str, Any], json.loads(RSAAlgorithm.to_jwk(key.public)))
    value.update({"alg": algorithm, "kid": key.kid, "use": "sig"})
    return value


def _jwks(*keys: _SigningKey) -> dict[str, Any]:
    return {"keys": [_jwk(key) for key in keys]}


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "azp": _CLIENT_ID,
        "sub": _SUBJECT,
        "iat": now - 10,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


def _token(key: _SigningKey, *, claims: dict[str, Any] | None = None, headers: dict[str, Any] | None = None) -> str:
    return jwt.encode(claims or _claims(), key.private, algorithm="RS256", headers={"kid": key.kid, **(headers or {})})


def _replace_header_without_resigning(token: str, **overrides: Any) -> str:
    """Make a deliberately malformed-header token for pre-signature rejection tests."""
    encoded_header, encoded_payload, encoded_signature = token.split(".")
    header = jwt.get_unverified_header(token) | overrides
    encoded_header = base64.urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode()).rstrip(b"=").decode()
    return ".".join((encoded_header, encoded_payload, encoded_signature))


def _token_response(token: Any, *, token_type: Any = "Bearer", **extra: Any) -> dict[str, Any]:
    return {"access_token": token, "token_type": token_type, **extra}


def _resolver(jwks_uri: str) -> AuthentikOidcPrincipalResolver:
    return AuthentikOidcPrincipalResolver(
        expected_issuer=_ISSUER,
        discovered_issuer=_ISSUER,
        jwks_uri=jwks_uri,
        signing_algorithms=["RS256"],
        client_id=_CLIENT_ID,
    )


@pytest.fixture(scope="module")
def signing_keys() -> tuple[_SigningKey, _SigningKey, _SigningKey]:
    return _new_signing_key("key-1"), _new_signing_key("key-2"), _new_signing_key("key-3")


@pytest.fixture
async def jwks_server(signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]):
    state = _JwksState(document=_jwks(signing_keys[0]))
    port = pick_free_port()
    async with serve_app(Starlette(routes=[Route("/jwks", state.serve)]), port=port):
        yield state, f"http://127.0.0.1:{port}/jwks"


async def test_resolves_only_issuer_and_subject_from_access_token(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    result = await _resolver(jwks_uri).resolve(
        _token_response(_token(signing_keys[0]), token_type="bEaReR", id_token="deliberately-not-a-jwt")
    )

    assert result == VerifiedOidcPrincipal(issuer=_ISSUER, subject=_SUBJECT)
    assert state.requests == 1


@pytest.mark.parametrize(
    ("token", "token_type"),
    [
        (None, "Bearer"),
        (b"bytes-are-not-a-token", "Bearer"),
        ("", "Bearer"),
        (" \t", "Bearer"),
        ("syntactically-valid-later", None),
        ("syntactically-valid-later", b"Bearer"),
        ("syntactically-valid-later", "bearer-token"),
        ("syntactically-valid-later", " Bearer"),
    ],
)
async def test_rejects_invalid_token_response_boundary(jwks_server, token: Any, token_type: Any) -> None:
    state, jwks_uri = jwks_server
    with pytest.raises(InvalidOidcPrincipal):
        await _resolver(jwks_uri).resolve(_token_response(token, token_type=token_type))
    assert state.requests == 0


async def test_requires_both_token_response_fields(jwks_server) -> None:
    state, jwks_uri = jwks_server
    resolver = _resolver(jwks_uri)
    with pytest.raises(InvalidOidcPrincipal):
        await resolver.resolve({"token_type": "Bearer"})
    with pytest.raises(InvalidOidcPrincipal):
        await resolver.resolve({"access_token": "token"})
    assert state.requests == 0


@pytest.mark.parametrize(
    "headers",
    [{}, {"kid": ""}, {"kid": 42}, {"kid": "key-1", "crit": []}, {"kid": "key-1", "crit": ["custom"], "custom": True}],
)
async def test_requires_nonblank_string_kid_and_no_crit(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey], headers: dict[str, Any]
) -> None:
    state, jwks_uri = jwks_server
    token = (
        _replace_header_without_resigning(_token(signing_keys[0]), **headers)
        if "kid" in headers and not isinstance(headers["kid"], str)
        else jwt.encode(_claims(), signing_keys[0].private, algorithm="RS256", headers=headers)
    )
    with pytest.raises(InvalidOidcPrincipal):
        await _resolver(jwks_uri).resolve(_token_response(token))
    assert state.requests == 0


@pytest.mark.parametrize("algorithm", ["HS256", "ES256", "PS256", "none"])
async def test_rejects_every_non_rs256_algorithm_before_jwks_lookup(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey], algorithm: str
) -> None:
    state, jwks_uri = jwks_server
    match algorithm:
        case "HS256":
            key: Any = "a-test-only-symmetric-secret-that-is-long-enough"
        case "ES256":
            key = ec.generate_private_key(ec.SECP256R1())
        case "PS256":
            key = signing_keys[0].private
        case "none":
            key = ""
        case _:
            raise AssertionError("unhandled test algorithm")
    token = jwt.encode(_claims(), key, algorithm=algorithm, headers={"kid": signing_keys[0].kid})

    with pytest.raises(InvalidOidcPrincipal):
        await _resolver(jwks_uri).resolve(_token_response(token))
    assert state.requests == 0


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://other-issuer.example/"),
        ("aud", "other-client"),
        ("aud", [_CLIENT_ID]),
        ("azp", "other-client"),
        ("azp", [_CLIENT_ID]),
        ("azp", 1),
        ("sub", ""),
        ("sub", "   "),
        ("sub", 1),
        ("iat", True),
        ("iat", "1"),
        ("iat", 1.5),
        ("exp", True),
        ("exp", "4102444800"),
        ("exp", 4102444800.5),
    ],
)
async def test_rejects_wrong_or_non_strict_claims(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey], claim: str, value: Any
) -> None:
    _state, jwks_uri = jwks_server
    with pytest.raises(InvalidOidcPrincipal):
        await _resolver(jwks_uri).resolve(_token_response(_token(signing_keys[0], claims=_claims(**{claim: value}))))


@pytest.mark.parametrize("missing_claim", ["iss", "aud", "azp", "sub", "iat", "exp"])
async def test_rejects_missing_required_identity_and_lifetime_claims(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey], missing_claim: str
) -> None:
    _state, jwks_uri = jwks_server
    claims = _claims()
    del claims[missing_claim]
    with pytest.raises(InvalidOidcPrincipal):
        await _resolver(jwks_uri).resolve(_token_response(_token(signing_keys[0], claims=claims)))


async def test_rejects_nonpositive_token_lifetime(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    _state, jwks_uri = jwks_server
    timestamp = int(time.time()) + 10
    with pytest.raises(InvalidOidcPrincipal):
        await _resolver(jwks_uri).resolve(
            _token_response(_token(signing_keys[0], claims=_claims(iat=timestamp, exp=timestamp)))
        )


@pytest.mark.parametrize(
    ("iat_offset", "exp_offset", "accepted"), [(-60, -29, True), (-60, -35, False), (29, 120, True), (35, 120, False)]
)
async def test_applies_thirty_second_clock_skew(
    jwks_server,
    signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey],
    iat_offset: int,
    exp_offset: int,
    accepted: bool,
) -> None:
    _state, jwks_uri = jwks_server
    now = int(time.time())
    resolution = _resolver(jwks_uri).resolve(
        _token_response(_token(signing_keys[0], claims=_claims(iat=now + iat_offset, exp=now + exp_offset)))
    )
    if accepted:
        assert await resolution == VerifiedOidcPrincipal(issuer=_ISSUER, subject=_SUBJECT)
    else:
        with pytest.raises(InvalidOidcPrincipal):
            await resolution


async def test_rejects_bad_signature(jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]) -> None:
    _state, jwks_uri = jwks_server
    token_with_forged_kid = _token(signing_keys[1], headers={"kid": signing_keys[0].kid})
    with pytest.raises(InvalidOidcPrincipal):
        await _resolver(jwks_uri).resolve(_token_response(token_with_forged_kid))


async def test_known_kid_uses_bounded_jwks_set_cache(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    resolver = _resolver(jwks_uri)
    response = _token_response(_token(signing_keys[0]))

    assert await resolver.resolve(response) == await resolver.resolve(response)
    assert state.requests == 1


async def test_concurrent_first_lookups_share_one_jwks_fetch(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    resolver = _resolver(jwks_uri)
    response = _token_response(_token(signing_keys[0]))

    results = await asyncio.gather(*(resolver.resolve(response) for _ in range(8)))

    assert results == [VerifiedOidcPrincipal(issuer=_ISSUER, subject=_SUBJECT)] * 8
    assert state.requests == 1


async def test_new_kid_forces_one_jwks_refresh(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    resolver = _resolver(jwks_uri)
    assert await resolver.resolve(_token_response(_token(signing_keys[0])))
    state.document = _jwks(signing_keys[1])

    result = await resolver.resolve(_token_response(_token(signing_keys[1])))

    assert result == VerifiedOidcPrincipal(issuer=_ISSUER, subject=_SUBJECT)
    assert state.requests == 2


async def test_rsa_jwk_without_optional_alg_uses_rs256(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    jwk = _jwk(signing_keys[0])
    del jwk["alg"]
    state.document = {"keys": [jwk]}

    result = await _resolver(jwks_uri).resolve(_token_response(_token(signing_keys[0])))

    assert result == VerifiedOidcPrincipal(issuer=_ISSUER, subject=_SUBJECT)


async def test_jwk_with_incompatible_algorithm_is_verification_unavailable(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    state.document = {"keys": [_jwk(signing_keys[0], algorithm="PS256")]}

    with pytest.raises(OidcPrincipalVerificationUnavailable):
        await _resolver(jwks_uri).resolve(_token_response(_token(signing_keys[0])))


async def test_unknown_kid_is_invalid_after_successful_refresh(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    with pytest.raises(InvalidOidcPrincipal):
        await _resolver(jwks_uri).resolve(_token_response(_token(signing_keys[1])))
    assert state.requests == 2


async def test_jwks_http_failure_is_verification_unavailable(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    state.status_code = 503
    with pytest.raises(OidcPrincipalVerificationUnavailable):
        await _resolver(jwks_uri).resolve(_token_response(_token(signing_keys[0])))


@pytest.mark.parametrize("malformed_document", [b"not-json", b"[]", b'{"keys":"not-a-list"}'])
async def test_malformed_jwks_is_verification_unavailable(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey], malformed_document: bytes
) -> None:
    state, jwks_uri = jwks_server
    state.raw_body = malformed_document
    with pytest.raises(OidcPrincipalVerificationUnavailable):
        await _resolver(jwks_uri).resolve(_token_response(_token(signing_keys[0])))


async def test_failed_rotation_refresh_is_verification_unavailable(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    resolver = _resolver(jwks_uri)
    assert await resolver.resolve(_token_response(_token(signing_keys[0])))
    state.status_code = 503

    with pytest.raises(OidcPrincipalVerificationUnavailable):
        await resolver.resolve(_token_response(_token(signing_keys[1])))
    assert state.requests == 2


async def test_public_errors_do_not_expose_token_or_claims(
    jwks_server, signing_keys: tuple[_SigningKey, _SigningKey, _SigningKey]
) -> None:
    state, jwks_uri = jwks_server
    private_subject = "do-not-leak-this-subject"
    invalid_token = _token(signing_keys[0], claims=_claims(sub=private_subject, aud="wrong"))
    with pytest.raises(InvalidOidcPrincipal) as invalid:
        await _resolver(jwks_uri).resolve(_token_response(invalid_token))
    state.status_code = 503
    with pytest.raises(OidcPrincipalVerificationUnavailable) as unavailable:
        await _resolver(jwks_uri).resolve(_token_response(_token(signing_keys[0], claims=_claims(sub=private_subject))))

    for error in (invalid.value, unavailable.value):
        rendered = f"{error!r} {error}"
        assert invalid_token not in rendered
        assert private_subject not in rendered


def test_constructor_rejects_discovery_or_configuration_mismatch() -> None:
    base: dict[str, Any] = {
        "expected_issuer": _ISSUER,
        "discovered_issuer": _ISSUER,
        "jwks_uri": "https://auth.example.test/jwks/",
        "signing_algorithms": ["RS256"],
        "client_id": _CLIENT_ID,
    }
    invalid_overrides: list[dict[str, Any]] = [
        {"expected_issuer": ""},
        {"discovered_issuer": None},
        {"discovered_issuer": f"{_ISSUER}/"},
        {"jwks_uri": None},
        {"jwks_uri": ""},
        {"jwks_uri": "file:///etc/passwd"},
        {"jwks_uri": "/relative/jwks"},
        {"signing_algorithms": None},
        {"signing_algorithms": "RS256"},
        {"signing_algorithms": []},
        {"signing_algorithms": ["PS256"]},
        {"signing_algorithms": ["RS256", 1]},
        {"client_id": ""},
        {"client_id": "  "},
    ]
    for override in invalid_overrides:
        with pytest.raises(ValueError, match=r"issuer|jwks|algorithm|client_id|RS256"):
            AuthentikOidcPrincipalResolver(**(base | override))


async def _authorization_and_refresh_tokens(
    client: httpx.AsyncClient, *, authorization_path: str, token_path: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    redirect_uri = "https://client.example.test/callback"
    authorized = await client.get(
        authorization_path,
        params={
            "client_id": _CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": "openid offline_access",
            "resource": "https://resource.example.test",
        },
    )
    code = parse_qs(urlparse(authorized.headers["location"]).query)["code"][0]
    issued = (
        await client.post(
            token_path,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": _CLIENT_ID,
                "redirect_uri": redirect_uri,
            },
        )
    ).json()
    refreshed = (
        await client.post(
            token_path,
            data={
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "client_id": _CLIENT_ID,
                "resource": "https://refreshed-resource.example.test",
            },
        )
    ).json()
    return issued, refreshed


async def test_authentik_compatible_mock_access_tokens_match_authentik_audience_contract() -> None:
    private_key, public_key = generate_rsa_keypair()
    port = pick_free_port()
    base_url = f"http://127.0.0.1:{port}"
    issuer = f"{base_url}/application/o/test/"
    app = build_mock_oidc_app(
        issuer_url=issuer, private_key=private_key, public_key=public_key, authentik_compatible=True
    )
    async with serve_app(app, port=port), httpx.AsyncClient(base_url=base_url, follow_redirects=False) as client:
        token_responses = await _authorization_and_refresh_tokens(
            client, authorization_path="/application/o/authorize/", token_path="/application/o/token/"
        )

    for token_response in token_responses:
        claims = jwt.decode(
            token_response["access_token"],
            public_key,
            algorithms=["RS256"],
            audience=_CLIENT_ID,
            issuer=issuer,
            options={"strict_aud": True},
        )
        assert claims["aud"] == _CLIENT_ID
        assert claims["azp"] == _CLIENT_ID


async def test_default_mock_access_token_audience_behavior_is_unchanged() -> None:
    private_key, public_key = generate_rsa_keypair()
    port = pick_free_port()
    issuer = f"http://127.0.0.1:{port}"
    app = build_mock_oidc_app(issuer_url=issuer, private_key=private_key, public_key=public_key)
    async with serve_app(app, port=port), httpx.AsyncClient(base_url=issuer, follow_redirects=False) as client:
        issued, refreshed = await _authorization_and_refresh_tokens(
            client, authorization_path="/authorize", token_path="/token"
        )

    issued_claims = jwt.decode(
        issued["access_token"],
        public_key,
        algorithms=["RS256"],
        audience="https://resource.example.test",
        issuer=issuer,
    )
    refreshed_claims = jwt.decode(
        refreshed["access_token"],
        public_key,
        algorithms=["RS256"],
        audience="https://refreshed-resource.example.test",
        issuer=issuer,
    )
    assert "azp" not in issued_claims
    assert "azp" not in refreshed_claims


if __name__ == "__main__":
    pytest_bazel.main()
