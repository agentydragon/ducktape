"""Tests for airlock.oauth.provider."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_bazel
import respx
from httpx import Response

from airlock.oauth.provider import (
    GenericOAuth2Provider,
    OAuth2ProviderConfig,
    TokenData,
    TokenSecretConfig,
    _parse_token_response,
    generate_pkce_pair,
)


@pytest.fixture
def provider_config() -> OAuth2ProviderConfig:
    return OAuth2ProviderConfig(
        name="test",
        display_name="Test Provider",
        authorize_url="https://example.com/authorize",
        token_url="https://example.com/token",
        scopes=["scope1", "scope2"],
        redirect_uri="http://localhost:8080/callback/test",
        refresh_secret=TokenSecretConfig(name="test-tokens"),
        access_secret=TokenSecretConfig(name="test-access-token"),
    )


@pytest.fixture
def provider(provider_config: OAuth2ProviderConfig) -> GenericOAuth2Provider:
    return GenericOAuth2Provider(
        provider_config, "test-client-id", "test-client-secret", "http://localhost/oauth/callback"
    )


def test_build_authorize_url(provider: GenericOAuth2Provider) -> None:
    url = provider.build_authorize_url("test-state")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.hostname == "example.com"
    assert parsed.path == "/authorize"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["test-client-id"]
    assert params["scope"] == ["scope1 scope2"]
    assert params["state"] == ["test-state"]


def test_explicit_redirect_uri_wins(provider: GenericOAuth2Provider) -> None:
    params = parse_qs(urlparse(provider.build_authorize_url("st")).query)
    assert params["redirect_uri"] == ["http://localhost:8080/callback/test"]


def test_redirect_uri_falls_back_to_shared_default() -> None:
    config = OAuth2ProviderConfig(
        name="new",
        display_name="New",
        authorize_url="https://example.com/authorize",
        token_url="https://example.com/token",
        scopes=["s"],
        refresh_secret=TokenSecretConfig(name="new-tokens"),
        access_secret=TokenSecretConfig(name="new-access"),
    )
    provider = GenericOAuth2Provider(config, "id", "sec", "https://airlock.example.com/oauth/callback")
    params = parse_qs(urlparse(provider.build_authorize_url("st")).query)
    assert params["redirect_uri"] == ["https://airlock.example.com/oauth/callback"]


def test_build_authorize_url_with_extra_params() -> None:
    config = OAuth2ProviderConfig(
        name="google",
        display_name="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["email"],
        redirect_uri="http://localhost/callback/google",
        refresh_secret=TokenSecretConfig(name="google-tokens"),
        access_secret=TokenSecretConfig(name="google-access-token"),
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
    )
    provider = GenericOAuth2Provider(config, "gid", "gsecret", "http://localhost/oauth/callback")
    url = provider.build_authorize_url("state123")
    params = parse_qs(urlparse(url).query)

    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["client_id"] == ["gid"]


def test_generate_state(provider: GenericOAuth2Provider) -> None:
    state1 = provider.generate_state()
    state2 = provider.generate_state()
    assert state1 != state2
    assert len(state1) > 20


@respx.mock
async def test_exchange_code(provider: GenericOAuth2Provider) -> None:
    route = respx.post("https://example.com/token").mock(
        return_value=Response(
            200,
            json={
                "access_token": "access-123",
                "refresh_token": "refresh-456",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "scope1 scope2",
            },
        )
    )
    token = await provider.exchange_code("auth-code-789")

    assert token.access_token == "access-123"
    assert token.refresh_token == "refresh-456"
    assert token.scope == "scope1 scope2"
    assert token.expires_at > datetime.now(UTC)
    assert route.called


def test_generate_pkce_pair_s256() -> None:
    """code_challenge must be base64url(SHA256(code_verifier)) without padding."""
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128  # RFC 7636 length window
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert challenge == expected
    assert "=" not in challenge  # padding stripped

    # Each call must yield a fresh pair.
    v2, c2 = generate_pkce_pair()
    assert v2 != verifier
    assert c2 != challenge


def test_build_authorize_url_with_pkce_and_aud() -> None:
    config = OAuth2ProviderConfig(
        name="bsc",
        display_name="BSC",
        authorize_url="https://example.com/authorize",
        token_url="https://example.com/token",
        scopes=["interop"],
        redirect_uri="http://localhost/callback/bsc",
        refresh_secret=TokenSecretConfig(name="bsc-tokens"),
        access_secret=TokenSecretConfig(name="bsc-access-token"),
        use_pkce=True,
        aud="https://fhir.example.com/r4",
    )
    provider = GenericOAuth2Provider(config, "cid", "csec", "http://localhost/oauth/callback")
    url = provider.build_authorize_url("st", code_challenge="CHAL")
    params = parse_qs(urlparse(url).query)

    assert params["code_challenge"] == ["CHAL"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["aud"] == ["https://fhir.example.com/r4"]
    assert params["scope"] == ["interop"]


def test_build_authorize_url_no_pkce_no_aud(provider: GenericOAuth2Provider) -> None:
    """When PKCE not requested and aud not configured, neither appears."""
    url = provider.build_authorize_url("state")
    params = parse_qs(urlparse(url).query)
    assert "code_challenge" not in params
    assert "code_challenge_method" not in params
    assert "aud" not in params


@respx.mock
async def test_exchange_code_with_pkce(provider: GenericOAuth2Provider) -> None:
    route = respx.post("https://example.com/token").mock(
        return_value=Response(200, json={"access_token": "a", "expires_in": 3600})
    )
    await provider.exchange_code("code", code_verifier="my-verifier")
    sent = dict(parse_qs(route.calls.last.request.content.decode()))
    assert sent["code_verifier"] == ["my-verifier"]
    assert sent["code"] == ["code"]


@respx.mock
async def test_exchange_code_no_pkce_omits_verifier(provider: GenericOAuth2Provider) -> None:
    route = respx.post("https://example.com/token").mock(
        return_value=Response(200, json={"access_token": "a", "expires_in": 3600})
    )
    await provider.exchange_code("code")
    sent = dict(parse_qs(route.calls.last.request.content.decode()))
    assert "code_verifier" not in sent


@respx.mock
async def test_refresh_tokens(provider: GenericOAuth2Provider) -> None:
    route = respx.post("https://example.com/token").mock(
        return_value=Response(
            200,
            json={
                "access_token": "new-access-123",
                "refresh_token": "new-refresh-456",
                "expires_in": 3600,
                "scope": "scope1",
            },
        )
    )
    token = await provider.refresh_tokens("old-refresh-token")

    assert token.access_token == "new-access-123"
    assert token.refresh_token == "new-refresh-456"
    assert route.called


@respx.mock
async def test_refresh_tokens_preserves_old_refresh_token(provider: GenericOAuth2Provider) -> None:
    """Google omits refresh_token on refresh responses."""
    respx.post("https://example.com/token").mock(
        return_value=Response(200, json={"access_token": "new-access", "expires_in": 3600, "scope": "scope1"})
    )
    token = await provider.refresh_tokens("my-precious-refresh-token")

    assert token.access_token == "new-access"
    assert token.refresh_token == "my-precious-refresh-token"


def test_needs_refresh_not_yet(provider: GenericOAuth2Provider) -> None:
    token = TokenData(access_token="a", refresh_token="r", expires_at=datetime.now(UTC) + timedelta(days=15), scope="s")
    assert not provider.needs_refresh(token)


def test_needs_refresh_soon(provider: GenericOAuth2Provider) -> None:
    token = TokenData(
        access_token="a", refresh_token="r", expires_at=datetime.now(UTC) + timedelta(minutes=30), scope="s"
    )
    assert provider.needs_refresh(token)


def test_needs_refresh_expired(provider: GenericOAuth2Provider) -> None:
    token = TokenData(access_token="a", refresh_token="r", expires_at=datetime.now(UTC) - timedelta(hours=1), scope="s")
    assert provider.needs_refresh(token)


def test_parse_token_response() -> None:
    data = {"access_token": "at", "refresh_token": "rt", "token_type": "Bearer", "expires_in": 7200, "scope": "read"}
    token = _parse_token_response(data)
    assert token.access_token == "at"
    assert token.refresh_token == "rt"
    assert token.expires_at > datetime.now(UTC)


def test_parse_token_response_missing_refresh_token() -> None:
    data = {"access_token": "at", "expires_in": 3600}
    token = _parse_token_response(data)
    assert token.access_token == "at"
    assert token.refresh_token == ""


if __name__ == "__main__":
    pytest_bazel.main()
