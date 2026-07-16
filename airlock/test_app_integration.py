"""Integration coverage for the Airlock OAuth-only application."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_bazel
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from airlock.app import create_app
from airlock.config import Settings
from airlock.oauth.k8s_client import K8sTokenStore
from airlock.oauth.provider import (
    GenericOAuth2Provider,
    OAuth2ProviderConfig,
    OAuthConfig,
    TokenData,
    TokenSecretConfig,
)
from util.net import pick_free_port
from util.testing.asgi import serve_app


@pytest.fixture
def rsa_key_pair() -> RSAKeyPair:
    return RSAKeyPair.generate()


@pytest.fixture
def operator_headers(rsa_key_pair: RSAKeyPair) -> dict[str, str]:
    token = rsa_key_pair.create_token(subject="operator", scopes=["openid"])
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def mock_k8s_store():
    """Patch in-cluster Secret storage for a non-Kubernetes test process."""
    store = MagicMock()
    store.delete_orphaned_secrets = AsyncMock()
    store.read_token = AsyncMock(return_value=None)
    with patch("airlock.app.K8sTokenStore.from_incluster", new_callable=AsyncMock, return_value=store):
        yield store


def _settings(port: int, *, oauth: OAuthConfig | None = None) -> Settings:
    return Settings(
        public_base_url=f"http://127.0.0.1:{port}",
        oidc_issuer="https://unused.example.com",
        oidc_client_id="airlock-test",
        oauth=oauth or OAuthConfig(providers=[]),
        port=port,
    )


async def test_oauth_api_works_after_startup(
    rsa_key_pair: RSAKeyPair, operator_headers: dict[str, str], mock_k8s_store: MagicMock
) -> None:
    port = pick_free_port()
    app = create_app(_settings(port), auth=JWTVerifier(public_key=rsa_key_pair.public_key), include_static=False)

    async with serve_app(app, port=port), httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        assert (await http.get("/healthz")).json() == {"ok": True}
        assert (await http.get("/auth/config")).json()["client_id"] == "airlock-test"
        response = await http.get("/api/oauth/providers", headers=operator_headers)

    assert response.status_code == 200
    assert response.json() == []
    mock_k8s_store.delete_orphaned_secrets.assert_awaited()


async def test_oauth_providers_reports_expired_token_status(
    rsa_key_pair: RSAKeyPair,
    monkeypatch: pytest.MonkeyPatch,
    mock_k8s_store: MagicMock,
    operator_headers: dict[str, str],
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
    port = pick_free_port()
    with patch("airlock.app.token_refresh_loop", side_effect=fake_token_refresh_loop):
        app = create_app(
            _settings(port, oauth=oauth), auth=JWTVerifier(public_key=rsa_key_pair.public_key), include_static=False
        )
        async with serve_app(app, port=port), httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            response = await http.get("/api/oauth/providers", headers=operator_headers)

    assert response.status_code == 200
    [provider] = response.json()
    assert provider["name"] == "test"
    assert provider["display_name"] == "Test Provider"
    assert provider["requested_scopes"] == ["daily", "sleep"]
    assert provider["status"]["state"] == "expired"
    assert provider["status"]["scope"] == "daily sleep"
    assert provider["status"]["last_refresh_error"] == "RuntimeError('refresh failed')"
    mock_k8s_store.read_token.assert_called_once_with("test-refresh", "airlock-test")


async def test_api_requires_valid_bearer_token(rsa_key_pair: RSAKeyPair, mock_k8s_store: MagicMock) -> None:
    port = pick_free_port()
    app = create_app(_settings(port), auth=JWTVerifier(public_key=rsa_key_pair.public_key), include_static=False)
    valid_token = rsa_key_pair.create_token(subject="operator", scopes=["openid"])

    async with serve_app(app, port=port), httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        assert (await http.get("/api/oauth/providers")).status_code == 401
        assert (
            await http.get("/api/oauth/providers", headers={"Authorization": "Bearer not-a-jwt"})
        ).status_code == 401
        assert (
            await http.get("/api/oauth/providers", headers={"Authorization": f"Bearer {valid_token}"})
        ).status_code == 200


async def test_mcp_and_tool_approval_routes_are_absent(
    rsa_key_pair: RSAKeyPair, operator_headers: dict[str, str], mock_k8s_store: MagicMock
) -> None:
    port = pick_free_port()
    app = create_app(_settings(port), auth=JWTVerifier(public_key=rsa_key_pair.public_key), include_static=False)

    async with serve_app(app, port=port), httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        assert (await http.post("/mcp", headers=operator_headers)).status_code == 404
        assert (await http.get("/api/actions", headers=operator_headers)).status_code == 404
        assert (await http.get("/api/backends", headers=operator_headers)).status_code == 404
        assert (await http.get("/api/events", headers=operator_headers)).status_code == 404
        assert (
            await http.post("/api/actions/00000000-0000-0000-0000-000000000001/1/approve", headers=operator_headers)
        ).status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()
