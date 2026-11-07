"""Tests for airlock.oauth.provider."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import pytest_bazel
from authlib.integrations.httpx_client import AsyncOAuth2Client as AuthlibAsyncOAuth2Client

import airlock.oauth.provider as provider_module
from airlock.oauth.config import OAuth2ProviderConfig, TokenSecretConfig
from airlock.oauth.provider import GenericOAuth2Provider, TokenData


def _mock_token_endpoint(monkeypatch: pytest.MonkeyPatch, response: httpx2.Response) -> list[httpx2.Request]:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://example.com/token"
        requests.append(request)
        return response

    original_client = provider_module.AsyncOAuth2Client

    def client_with_mock_transport(*args: Any, **kwargs: Any) -> AuthlibAsyncOAuth2Client:
        return original_client(*args, transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(provider_module, "AsyncOAuth2Client", client_with_mock_transport)
    return requests


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


async def test_build_authorize_url(provider: GenericOAuth2Provider) -> None:
    url, code_verifier = await provider.build_authorize_url("test-state")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.hostname == "example.com"
    assert parsed.path == "/authorize"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["test-client-id"]
    assert params["scope"] == ["scope1 scope2"]
    assert params["state"] == ["test-state"]
    assert code_verifier is None


async def test_explicit_redirect_uri_wins(provider: GenericOAuth2Provider) -> None:
    url, _ = await provider.build_authorize_url("st")
    params = parse_qs(urlparse(url).query)
    assert params["redirect_uri"] == ["http://localhost:8080/callback/test"]


async def test_redirect_uri_falls_back_to_shared_default() -> None:
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
    url, _ = await provider.build_authorize_url("st")
    params = parse_qs(urlparse(url).query)
    assert params["redirect_uri"] == ["https://airlock.example.com/oauth/callback"]


async def test_build_authorize_url_with_extra_params() -> None:
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
    url, _ = await provider.build_authorize_url("state123")
    params = parse_qs(urlparse(url).query)

    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["client_id"] == ["gid"]


def test_generate_state(provider: GenericOAuth2Provider) -> None:
    state1 = provider.generate_state()
    state2 = provider.generate_state()
    assert state1 != state2
    assert len(state1) > 20


async def test_exchange_code(provider: GenericOAuth2Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_token_endpoint(
        monkeypatch,
        httpx2.Response(
            200,
            json={
                "access_token": "access-123",
                "refresh_token": "refresh-456",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "scope1 scope2",
            },
        ),
    )
    token = await provider.exchange_code("auth-code-789")

    assert token.access_token == "access-123"
    assert token.refresh_token == "refresh-456"
    assert token.scope == "scope1 scope2"
    assert token.expires_at > datetime.now(UTC)
    assert len(requests) == 1
    sent = dict(parse_qs(requests[0].content.decode()))
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["client_id"] == ["test-client-id"]
    assert sent["client_secret"] == ["test-client-secret"]
    assert sent["redirect_uri"] == ["http://localhost:8080/callback/test"]
    assert "Authorization" not in requests[0].headers


async def test_build_authorize_url_with_pkce_and_aud() -> None:
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
    url, code_verifier = await provider.build_authorize_url("st")
    params = parse_qs(urlparse(url).query)

    assert code_verifier is not None
    assert 43 <= len(code_verifier) <= 128
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    )
    assert params["code_challenge"] == [expected_challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["aud"] == ["https://fhir.example.com/r4"]
    assert params["scope"] == ["interop"]


async def test_build_authorize_url_no_pkce_no_aud(provider: GenericOAuth2Provider) -> None:
    """When PKCE not requested and aud not configured, neither appears."""
    url, code_verifier = await provider.build_authorize_url("state")
    params = parse_qs(urlparse(url).query)
    assert code_verifier is None
    assert "code_challenge" not in params
    assert "code_challenge_method" not in params
    assert "aud" not in params


async def test_exchange_code_with_pkce(provider: GenericOAuth2Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_token_endpoint(monkeypatch, httpx2.Response(200, json={"access_token": "a", "expires_in": 3600}))
    await provider.exchange_code("code", code_verifier="my-verifier")
    sent = dict(parse_qs(requests[0].content.decode()))
    assert sent["code_verifier"] == ["my-verifier"]
    assert sent["code"] == ["code"]


async def test_exchange_code_no_pkce_omits_verifier(
    provider: GenericOAuth2Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _mock_token_endpoint(monkeypatch, httpx2.Response(200, json={"access_token": "a", "expires_in": 3600}))
    await provider.exchange_code("code")
    sent = dict(parse_qs(requests[0].content.decode()))
    assert "code_verifier" not in sent


async def test_exchange_code_preserves_http_status_errors(
    provider: GenericOAuth2Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_token_endpoint(monkeypatch, httpx2.Response(400, json={"error": "invalid_grant"}))

    with pytest.raises(httpx2.HTTPStatusError) as exc_info:
        await provider.exchange_code("invalid-code")

    assert exc_info.value.response.status_code == 400


async def test_refresh_tokens(provider: GenericOAuth2Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_token_endpoint(
        monkeypatch,
        httpx2.Response(
            200,
            json={
                "access_token": "new-access-123",
                "refresh_token": "new-refresh-456",
                "expires_in": 3600,
                "scope": "scope1",
            },
        ),
    )
    token = await provider.refresh_tokens("old-refresh-token")

    assert token.access_token == "new-access-123"
    assert token.refresh_token == "new-refresh-456"
    assert len(requests) == 1
    sent = dict(parse_qs(requests[0].content.decode()))
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == ["old-refresh-token"]
    assert sent["client_id"] == ["test-client-id"]
    assert sent["client_secret"] == ["test-client-secret"]
    assert "Authorization" not in requests[0].headers


async def test_refresh_tokens_preserves_old_refresh_token(
    provider: GenericOAuth2Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Google omits refresh_token on refresh responses."""
    _mock_token_endpoint(
        monkeypatch, httpx2.Response(200, json={"access_token": "new-access", "expires_in": 3600, "scope": "scope1"})
    )
    token = await provider.refresh_tokens("my-precious-refresh-token")

    assert token.access_token == "new-access"
    assert token.refresh_token == "my-precious-refresh-token"


def test_authlib_client_uses_httpx2_backend() -> None:
    assert issubclass(AuthlibAsyncOAuth2Client, httpx2.AsyncClient)


def test_needs_refresh_soon(provider: GenericOAuth2Provider) -> None:
    token = TokenData(
        access_token="a", refresh_token="r", expires_at=datetime.now(UTC) + timedelta(minutes=30), scope="s"
    )
    assert provider.needs_refresh(token)


def test_needs_refresh_expired(provider: GenericOAuth2Provider) -> None:
    token = TokenData(access_token="a", refresh_token="r", expires_at=datetime.now(UTC) - timedelta(hours=1), scope="s")
    assert provider.needs_refresh(token)


if __name__ == "__main__":
    pytest_bazel.main()
