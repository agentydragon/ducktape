"""Integration coverage for the Airlock OAuth-only application."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_bazel
from itsdangerous import TimestampSigner

from airlock.app import create_app
from airlock.auth import session_cookie_name
from airlock.oauth.config import OAuth2ProviderConfig, OAuthConfig, TokenSecretConfig
from airlock.oauth.k8s_client import K8sTokenStore
from airlock.oauth.provider import ACCESS_TOKEN_FIELDS, GenericOAuth2Provider, TokenData
from airlock.settings import Settings
from util.testing.asgi import BoundHTTPServer, bind_http_server


@pytest.fixture
def mock_k8s_store():
    """Patch in-cluster Secret storage for a non-Kubernetes test process."""
    store = MagicMock()
    store.delete_orphaned_secrets = AsyncMock()
    store.read_token = AsyncMock(return_value=None)
    with patch("airlock.app.K8sTokenStore.from_incluster", new_callable=AsyncMock, return_value=store):
        yield store


@pytest.fixture
def endpoint() -> Iterator[BoundHTTPServer]:
    with bind_http_server() as endpoint:
        yield endpoint


@pytest.fixture
def settings(endpoint: BoundHTTPServer) -> Settings:
    return Settings(
        public_base_url=endpoint.origin,
        oidc_issuer="https://unused.example.com",
        oidc_client_id="airlock-test",
        oidc_client_secret="airlock-test-secret",
        oidc_session_secret="airlock-test-session-secret",
        oauth=OAuthConfig(providers=[]),
        port=endpoint.port,
    )


@pytest.fixture
async def http(
    endpoint: BoundHTTPServer, settings: Settings, mock_k8s_store: MagicMock
) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        endpoint.serve(create_app(settings, include_static=False)),
        httpx.AsyncClient(base_url=endpoint.origin) as http,
    ):
        yield http


def _operator_cookies(settings: Settings, subject: str = "operator") -> dict[str, str]:
    session = {
        "user": {
            "issuer": settings.oidc_issuer,
            "subject": subject,
            "username": subject,
            "expires_at": time.time() + 3600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode("utf-8"))
    # Match SessionMiddleware's default TimestampSigner configuration.
    signer = TimestampSigner(settings.oidc_session_secret.get_secret_value())
    return {session_cookie_name(settings): signer.sign(payload).decode("ascii")}


async def test_oauth_api_works_after_startup(
    http: httpx.AsyncClient, settings: Settings, mock_k8s_store: MagicMock
) -> None:
    assert (await http.get("/healthz")).json() == {"ok": True}
    http.cookies.update(_operator_cookies(settings))
    response = await http.get("/api/oauth/providers")

    assert response.status_code == 200
    assert response.json() == []
    mock_k8s_store.delete_orphaned_secrets.assert_awaited()


async def test_oauth_providers_reports_expired_token_status(
    monkeypatch: pytest.MonkeyPatch, mock_k8s_store: MagicMock, endpoint: BoundHTTPServer, settings: Settings
) -> None:
    monkeypatch.setenv("TEST_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("TEST_CLIENT_SECRET", "test-client-secret")
    expired_token = TokenData(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        scope="daily sleep",
    )
    mock_k8s_store.read_token = AsyncMock(return_value=expired_token)

    def fake_token_refresh_loop(
        providers: Mapping[str, GenericOAuth2Provider],
        k8s_store: K8sTokenStore,
        target_namespace: str,
        check_interval: float = 300,
        refresh_errors: dict[str, str] | None = None,
    ):
        assert refresh_errors is not None
        refresh_errors["test"] = "RuntimeError('refresh failed')"

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        return wait_forever()

    oauth = OAuthConfig(
        target_namespace="airlock-test",
        providers=[
            OAuth2ProviderConfig(
                name="test",
                display_name="Test Provider",
                authorize_url="https://example.com/authorize",
                token_url="https://example.com/token",
                scopes=["daily", "sleep"],
                redirect_uri="https://example.com/callback/test",
                refresh_secret=TokenSecretConfig(name="test-refresh"),
                access_secret=TokenSecretConfig(name="test-access"),
            )
        ],
    )
    with patch("airlock.app.token_refresh_loop", side_effect=fake_token_refresh_loop):
        settings = settings.model_copy(update={"oauth": oauth})
        app = create_app(settings, include_static=False)
        async with endpoint.serve(app), httpx.AsyncClient(base_url=endpoint.origin) as http:
            http.cookies.update(_operator_cookies(settings))
            response = await http.get("/api/oauth/providers")

    assert response.status_code == 200
    [provider] = response.json()
    assert provider["name"] == "test"
    assert provider["display_name"] == "Test Provider"
    assert provider["requested_scopes"] == ["daily", "sleep"]
    assert provider["status"]["state"] == "expired"
    assert provider["status"]["scope"] == "daily sleep"
    assert provider["status"]["last_refresh_error"] == "RuntimeError('refresh failed')"
    mock_k8s_store.read_token.assert_called_once_with("test-refresh", "airlock-test")


async def test_api_requires_valid_session(http: httpx.AsyncClient, settings: Settings) -> None:
    assert (await http.get("/api/oauth/providers")).status_code == 401
    assert (await http.get("/api/oauth/providers", headers={"Authorization": "Bearer not-a-jwt"})).status_code == 401
    http.cookies.update(_operator_cookies(settings))
    assert (await http.get("/api/oauth/providers")).status_code == 200


async def test_oauth_callback_keeps_pkce_state_bound_to_operator(
    monkeypatch: pytest.MonkeyPatch, endpoint: BoundHTTPServer, settings: Settings, mock_k8s_store: MagicMock
) -> None:
    monkeypatch.setenv("TEST_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("TEST_CLIENT_SECRET", "test-client-secret")
    mock_k8s_store.write_token = AsyncMock()
    token = TokenData(
        access_token="access-token",
        refresh_token="refresh-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="read",
    )
    exchange_calls: list[tuple[str, str | None]] = []

    async def exchange_code(_provider: GenericOAuth2Provider, code: str, code_verifier: str | None = None) -> TokenData:
        exchange_calls.append((code, code_verifier))
        return token

    monkeypatch.setattr(GenericOAuth2Provider, "exchange_code", exchange_code)

    oauth = OAuthConfig(
        target_namespace="airlock-test",
        providers=[
            OAuth2ProviderConfig(
                name="test",
                display_name="Test Provider",
                authorize_url="https://example.com/authorize",
                token_url="https://example.com/token",
                scopes=["read"],
                refresh_secret=TokenSecretConfig(name="test-refresh"),
                access_secret=TokenSecretConfig(name="test-access"),
                use_pkce=True,
            )
        ],
    )
    settings = settings.model_copy(update={"oauth": oauth})

    app = create_app(settings, include_static=False)
    async with endpoint.serve(app), httpx.AsyncClient(base_url=endpoint.origin) as http:
        http.cookies.update(_operator_cookies(settings))
        first_start = await http.post("/oauth/authorize/test", headers={"Origin": settings.public_base_url})
        assert first_start.status_code == 303
        first_params = parse_qs(urlparse(first_start.headers["location"]).query)

        http.cookies.update(_operator_cookies(settings, subject="another-operator"))
        denied = await http.get("/oauth/callback", params={"code": "auth-code", "state": first_params["state"][0]})
        assert denied.status_code == 400
        assert not exchange_calls
        mock_k8s_store.write_token.assert_not_awaited()

        http.cookies.update(_operator_cookies(settings))
        start = await http.post("/oauth/authorize/test", headers={"Origin": settings.public_base_url})
        assert start.status_code == 303
        authorize_params = parse_qs(urlparse(start.headers["location"]).query)
        response = await http.get(
            "/oauth/callback", params={"code": "auth-code", "state": authorize_params["state"][0]}
        )

    assert response.status_code == 307
    assert len(exchange_calls) == 1
    assert exchange_calls[0][0] == "auth-code"
    verifier = exchange_calls[0][1]
    assert verifier is not None
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    )
    assert authorize_params["code_challenge"] == [expected_challenge]
    assert [call.args[0] for call in mock_k8s_store.write_token.await_args_list] == ["test-refresh", "test-access"]
    assert mock_k8s_store.write_token.await_args_list[0].args[1] == "airlock-test"
    assert mock_k8s_store.write_token.await_args_list[0].args[2] == token
    assert mock_k8s_store.write_token.await_args_list[1].args[2] == token
    assert mock_k8s_store.write_token.await_args_list[1].kwargs["fields"] == ACCESS_TOKEN_FIELDS


async def test_mcp_and_tool_approval_routes_are_absent(http: httpx.AsyncClient) -> None:
    assert (await http.post("/mcp")).status_code == 404
    assert (await http.get("/api/actions")).status_code == 404
    assert (await http.get("/api/backends")).status_code == 404
    assert (await http.get("/api/events")).status_code == 404
    assert (await http.post("/api/actions/00000000-0000-0000-0000-000000000001/1/approve")).status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()
